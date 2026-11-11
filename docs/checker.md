# The checker: how it works

The rules the checker (step 3 of [design.md](design.md)) is built to. [SPEC.md](../SPEC.md) says what it must
produce; this says how it gets there. Where this file and the code disagree, one of them is a bug.

## Shape

- A Python package in `checker/` (its own `pyproject.toml`, managed with uv), import name `anystatus`, command
  `anystatus-checker`. Python 3.11 or newer.
- **The core is standard library only:** `tomllib`, `asyncio`, `sqlite3`, `http.server`, `subprocess`. Drivers
  that only some checks need (MariaDB/MySQL, PostgreSQL) are optional extras: `uv add "mmm-anystatus-checker[postgres]"`.
- Commands:
  - `anystatus-checker run config.toml`: the service.
  - `anystatus-checker validate config.toml`: check the config and exit; lists *all* problems, not just the first.
  - `anystatus-checker once config.toml [--id ID]`: run checks once, print the document, exit. For writing and
    debugging a check without waiting for intervals.

## The config file

```toml
[checker]
name = "home-checker"          # becomes the document's `source`
listen = "127.0.0.1:8099"      # localhost by default; results can reveal internal names
state = "anystatus.sqlite"     # relative to the config file

[defaults]                     # apply to every check unless it says otherwise
interval = "5m"
confirm = 2

[[check]]
id = "backup.nas"
type = "sql_age"
label = "NAS backup"
group = "backups"
interval = "15m"
warn = "26h"                   # one missed nightly run is amber...
crit = "50h"                   # ...two is red: data is at risk
database = "sqlite:///var/lib/backup/log.db"
query = "SELECT max(finished_at) FROM runs WHERE ok = 1"
```

- **Durations** are a number of seconds or a string: `"30s"`, `"5m"`, `"2h"`, `"1d"`, also combined (`"1h30m"`).
- **Unknown keys are an error**, so a typo (`intervall`) can't silently fall back to a default.
- **Secrets never sit in the file.** Any key ending in `_env` names an environment variable to read the value from:
  `password_env = "BACKUP_DB_PASSWORD"`. A variable that isn't set is reported by `validate` and at startup, and the
  check's result is `unknown` ("environment variable BACKUP_DB_PASSWORD not set"), so it shows up rather than
  crashing the service.
- An invalid config stops the service from starting, with every problem listed. A running service is never
  half-configured.

## One run of a check

Each check type gives back an **observation**. That is one of:

- a **value** (number), which the limits turn into a level;
- a **level of its own** with a summary (`docker_status`, `exec` with Nagios exit codes, a custom function that
  decides itself);
- a **failure**: the check couldn't find out (timeout, refused connection, exception). Always `unknown`, never `crit`
  (SPEC: whether "can't reach the database" is critical is another check's business).

### Limits

`warn` and `crit` are thresholds on the value; reaching one counts (`value >= crit` is red when higher is worse).
Each check type knows which direction is worse for its value (`sql_age`, `disk` used: higher; `tls_expiry` days
left: lower); `worse = "above" | "below"` overrides it. Either limit may be left out. If both are given they must be
in order for the direction (`warn < crit` for "above"), otherwise the config is invalid. Duration-valued checks
(`sql_age`, `file_age`) accept durations as limits.

### Failure summaries never leak secrets

A failure's summary is short and built by the check type ("connection refused", "query timed out after 10 s"),
not the raw exception text, which can contain connection strings or passwords. The full error goes to the log at
debug level, also with secrets masked.

## Scheduling

- Every check runs once at startup (spread over the first few seconds, not all at once), then every `interval`,
  measured from start to start.
- **Never two runs of the same check at once.** A run that takes longer than its interval delays the next one; runs
  don't pile up.
- Each run has a `timeout` (default 10 s, never more than the interval). Blocking work (drivers, a custom function)
  runs in a worker thread. A thread can't be killed: when it times out, the run counts as a failure, and **the check
  isn't started again while that thread is still alive**. Its result then simply ages out (grey), which is the
  honest answer for a check that hangs.
- A result's `ts` is when its run finished.

## Debounce: `confirm`

A check that flips back and forth must not make the display flicker, so a new level has to be seen **`confirm`
runs in a row** before it is reported. Default 2; for a daily check, set `confirm = 1` (otherwise a problem takes a
day longer to show).

- While a change is waiting for confirmation, the checker keeps reporting the **last confirmed result** (level,
  summary, value), with the new `ts`. The check did run; it just hasn't changed its mind yet.
- Failures (`unknown`) are confirmed the same way, so one dropped ping doesn't turn the card grey.
- When a change is confirmed, its `since` is when the streak **started** (the first run that saw the new level),
  not when it was confirmed: the display should say when the problem began.
- A change of value or summary within the same level changes nothing about `since`.

## `since`, `ttl` and `ts`

- `ttl` defaults to 3 × interval (SPEC). Settable per check.
- `since` only moves when the reported level changes.

## Memory: the SQLite file

One row per check id, written after every run (one transaction):

| Column | |
|---|---|
| `id` | the check's id |
| `result` | the last reported result (JSON, as served) |
| `since` | when the reported level started |
| `candidate_level`, `candidate_count`, `candidate_since` | the debounce streak, if a change is pending |
| `updated` | when this row was written |

On startup:

- A check with a stored row starts from it, **but only if its stored result is still within its `ttl`**. If the
  checker was down longer than that, nobody knows what happened in between, so continuity is broken: the stored
  result is still served (it is honest, its `ts` is old and the display turns it grey), but `since` and the debounce
  streak start fresh with the next run.
- A check without a stored row is served as `unknown`, summary "waiting for the first run", `ts` = the time the
  checker started, its normal `ttl`. That is a true statement made at startup, and it keeps a fresh start from
  looking like "no checks reported".
- Rows of checks no longer in the config are deleted. A renamed id is a new check.
- The file can be deleted at any time; the cost is losing `since` and pending debounces.

The file uses WAL mode and is only ever touched by the one checker process.

## Serving

- A small HTTP server in a thread answers `GET /results` from an in-memory snapshot that the scheduler replaces
  after every run (SPEC: answering never runs checks). `GET /` is a plain-text or minimal HTML overview for humans.
- The document's `source` is `[checker] name`; `results` in config order.
- Anything else: 404. No other methods until push (`POST /results`) is designed.

## Logs

To stderr, one line per level change ("backup.nas: ok -> crit (last backup 3 days ago)"), per failure at info,
per run at debug. No log file of its own: the service manager or Docker keeps logs.

## Tests

pytest, ruff, and type hints checked by a type checker (pyright or mypy, whichever is lighter in CI). The scheduler
and debounce take a clock and run without sleeping, so the rules above (confirm streaks, `since` from the start of
a streak, restart continuity within/past `ttl`, a hanging thread not restarted) each have a test that runs in
milliseconds. CI runs on Python 3.11 and 3.14.

## First check types (step 3)

- `ping`: runs the system `ping` command (no raw sockets, so no root needed); value: round-trip time in ms.
  Unreachable is `crit`, not a failure: "the host doesn't answer" *is* the answer.
- `sql_age`: the age of a timestamp a query returns; SQLite in the core, other databases through the extras. An
  empty answer (`NULL`) is `crit` ("no rows"), unless `empty = "ok"`.
- `python`: a custom function, `function = "my_checks:backup_ok"` with an optional `path` to import from. It gets
  the check's config table and returns a number (compared with the limits), or `anystatus.Observation(level=...,
  summary=..., value=..., unit=...)`. An exception is a failure. Sync or async functions both work.
