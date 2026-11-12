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
show_after = { unknown = "10m" }   # one failed run doesn't turn a card grey

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

## `show_after`: how long a level must last before it shows

A check that flips back and forth must not make the display flicker, and some problems only matter once they last.
So each level can have a time:

```toml
show_after = { warn = "14d", crit = "7d", unknown = "2m" }
```

- **A level shows once it has lasted its time.** `ok`, `warn` and `crit` are ordered: a run that sees `crit` also
  counts towards `warn`. Until a level's time has passed, **the highest lower level whose time has passed** is
  shown, with this run's summary and value. With `{ warn = "0d", crit = "7d" }`, a check that keeps seeing `crit` is
  amber at once and red after 7 days; with `{ warn = "14d", crit = "7d" }` it is green for 7 days and then red
  (amber's own time hasn't passed by then). Green has no time: it always qualifies.
- **Getting better is reported at once** (green is green). That follows from the rule: a better level's streak is
  at least as long as the worse one's. Flapping around a limit is still absorbed, because the worse level has to
  last its time again.
- **`unknown` (a failure) has no lower level:** until its time has passed, the last shown result stays, with the
  new `ts` (the check did run; it just couldn't find out). A failure neither breaks nor extends the `warn` and `crit`
  streaks: that run saw nothing.
- **Shown at the first run at or after the time has passed:** never early, at most one `interval` late. Times are
  measured between the *starts* of runs, which are at least an interval apart, so a slow run doesn't push it one more
  interval. A time shorter than the interval means "at the next run"; the config warns about it, as it does about a
  `timeout` longer than the interval (a check's own times against its interval, `[defaults]`' against the default
  interval).
- **Units:** `s`, `m`, `h`, `d` only, also combined (`"1d12h"`); no weeks, months or years (a month is `"30d"`), and
  no bare numbers. `"0d"` is at once; `"never"` skips the level (written out, not implied by a missing key).
- **No default:** a level without a time shows at once. `[defaults]` can hold `show_after`; a check's own table
  replaces it per level (the levels it doesn't name keep the default's).
- `since`: a level that shows because things got worse starts at its streak, when the problem **began** (the first
  run that saw it), not when it was shown. A level that shows because things got better starts at this run. A
  change of value or summary within the same shown level changes nothing about `since`.
- The first run of a check is no exception: with `crit = "5m"`, a first run that sees `crit` shows green (with its
  summary) until the streak has lasted 5 minutes.
- `confirm`, the count of runs it replaces, is a config error that names `show_after`. A time rather than a count
  means the same in every check type and in `image_vulns`' schedule, and keeps its meaning when the `interval`
  changes.

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
| `warn_since`, `crit_since`, `unknown_since` | the streaks `show_after` measures: since when runs saw warn or worse, crit, failures |
| `updated` | when this row was written |
| `extra`, `extra_updated` | what a check type keeps for itself (`CheckMemory`: mqtt's last message, `image_vulns`' first-found dates) |

On startup:

- A check with a stored row starts from it, **but only if its stored result is still within its `ttl`**. If the
  checker was down longer than that, nobody knows what happened in between, so continuity is broken: the stored
  result is still served (it is honest, its `ts` is old and the display turns it grey), but `since` and the streaks
  start fresh with the next run. The same goes for a check type's own data (`extra`).
- A file written by a checker that still counted `confirm` runs (schema version 1) is converted: results, `since`
  and `extra` carry over, and the streaks of each shown level start at its `since`, so no card changes.
- A check without a stored row is served as `unknown`, summary "waiting for the first run", `ts` = the time the
  checker started, its normal `ttl`. That is a true statement made at startup, and it keeps a fresh start from
  looking like "no checks reported".
- Rows of checks no longer in the config are deleted. A renamed id is a new check.
- The file can be deleted at any time; the cost is losing `since`, the streaks and the check types' own data.

The file uses WAL mode and is only ever touched by the one checker process.

## Serving

- A small HTTP server in a thread answers `GET /results` from an in-memory snapshot that the scheduler replaces
  after every run (SPEC: answering never runs checks). `GET /` is a plain-text overview for humans, which also
  names the pages below.
- **`GET /vulns`** (only when an `image_vulns` check is configured): an HTML page with the full list of each
  `image_vulns` check's last run: level now, severity, image, CVE (linked to Trivy's page for it), package,
  installed and fixed version, class, first found. What is due comes first. A display can't be clicked, so this is
  where "1 critical, fixed, please update image" is explained. Rendered from what the last run left; "No run yet."
  before the first.
- The document's `source` is `[checker] name`; `results` in config order.
- Anything else: 404. No other methods until push (`POST /results`) is designed.

## Logs

To stderr, one line per level change ("backup.nas: ok -> crit (last backup 3 days ago)"), per failure at info,
per run at debug. No log file of its own: the service manager or Docker keeps logs.

## Tests

pytest, ruff, and type hints checked by a type checker (pyright or mypy, whichever is lighter in CI). The scheduler
and `show_after` take a clock and run without sleeping, so the rules above (`show_after` streaks, `since` from the
start of a streak, restart continuity within/past `ttl`, a hanging thread not restarted) each have a test that runs
in milliseconds. CI runs on Python 3.11 and 3.14.

## Check types

### Keys every check has

| Key | Default | |
|---|---|---|
| `id`, `type` | required | |
| `label`, `group` | `id`, none | as in SPEC |
| `interval` | `[defaults]` | how often it runs |
| `timeout` | 10 s | per run, at most `interval`; some types need more (`image_vulns`) |
| `ttl` | 3 × `interval` | |
| `show_after` | `[defaults]` per level, else at once | see [`show_after`](#show_after-how-long-a-level-must-last-before-it-shows) |
| `warn`, `crit`, `worse` | per type | see [Limits](#limits); some types have default limits |
| `summary` | per type | a template replacing the type's own text: `"last backup {age} ago"`. Placeholders: `{value}`, `{unit}`, `{age}` (a duration, written as "3.1 d") and the type's own (listed per type) |

**Durations as values** (ages, `sql_age`, `file_age`) are compared in seconds but reported in the largest unit that
fits, one decimal: `3.1` `d`, `5.0` `h`, `12.0` `min`. Limits on them are written as durations: `warn = "26h"`.

**Where things are reached:** hosts, URLs and paths are as the *checker* sees them. In a container, `localhost` is
the container itself.

### Overview

| Type | Value | Worse | Default limits | Needs | Step |
|---|---|---|---|---|---|
| `ping` | round trip, ms | above | none; no answer is `crit` | the system `ping` | 3 |
| `sql_age` | age of a timestamp | above | none | driver extra for MariaDB/MySQL/PostgreSQL | 3 |
| `python` | your function's | your function's | none | | 3 |
| `tcp` | connect time, ms | above | none; refused is `crit` | | 4 |
| `http` | response time, or a value from the JSON | above | none; a wrong status is `crit` | | 4 |
| `tls_expiry` | days until expiry | below | warn 21 d, crit 7 d | | 4 |
| `sql_value` | a number from a query | above | none | as `sql_age` | 4 |
| `file_age` | age of the newest matching file | above | none; no file is `crit` | | 4 |
| `disk` | used % | above | warn 85, crit 95 | | 4 |
| `exec` | from the plugin's output | | none; the exit code is the level | the command | 4 |
| `docker_status` | containers not fine | above | none; it decides its own level | Docker API access | 4 |
| `docker_updates` | images with updates | above | warn 1, never `crit` | Docker API, registry access | 4 |
| `image_vulns` | CVEs that are due | | its `schedule` decides | Trivy | 4 |
| `result_file` | as the file says | | | | 4 |
| `mqtt` | age of the last message, a value from it, or a level it names | above | none | `mqtt` extra, a broker | 4 |
| `system` | temperature, load, memory used | above | none | Linux | 4 |
| `systemd` *(proposed)* | | | | systemd on the host | later |
| `dns` *(proposed)* | lookup time, ms | above | none; no answer is `crit` | | later |

All types are standard library only, except the database drivers and the commands they run (`ping`, Trivy, your
`exec` plugin).

### `ping`

```toml
[[check]]
id = "ping.router"
type = "ping"
label = "Router"
group = "network"
interval = "1m"
host = "router.example"
count = 3          # packets per run (default 3)
warn = 50          # ms
crit = 200
```

Runs the system `ping` command, so no raw sockets and no root; its output is parsed for Linux (iputils, BusyBox) and
Windows. Value: average round trip. **No answer at all is `crit`** ("no answer"): that *is* the answer, not a
failure of the check. Partial loss is mentioned in the summary ("2/3 answered, 4.1 ms"). In a container the image
must allow unprivileged ping (`net.ipv4.ping_group_range`, or BusyBox ping with `CAP_NET_RAW`).

### `tcp`

```toml
[[check]]
id = "tcp.mariadb"
type = "tcp"
label = "MariaDB port"
host = "db.example"
port = 3306
```

Value: time to connect. Refused or no answer within the timeout is `crit` ("port 3306 refused"). It proves
something listens, nothing more.

### `http`

```toml
[[check]]
id = "http.sync"
type = "http"
label = "Photo sync"
url = "https://sync.example/api/status"
username = "monitor"
password_env = "SYNC_PASSWORD"          # or: bearer_env = "SYNC_TOKEN"
expect_status = [200]                    # default: any 2xx
json = "jobs.photos.last_success"        # optional: a value from the JSON answer
value_is = "time"                        # "number" (default) or "time": the value is a timestamp, its age is compared
warn = "2h"
crit = "1d"
```

Without `json`, the value is the response time in ms and the check is mostly about the status code. **An unexpected
status is `crit`** ("HTTP 503"), a connection that fails is `unknown`.

- `json` is a dotted path, with list indices as numbers (`items.0.age`).
- A missing path, or a value that isn't a number or a time, is a failure: the API changed, so the check can't tell.
- `value_is = "time"` accepts RFC 3339 strings and Unix seconds. A time without an offset is read as UTC unless
  `timezone = "Europe/Brussels"`.
- TLS is verified; `verify_tls = false` is allowed but logged as a warning at startup.
- Extra headers: a `headers` table. A key ending in `_env` takes its value from the environment, like every other
  key (`X-Api-Key_env = "SYNC_KEY"` sends `X-Api-Key`).
- **Redirects are not followed**: a `301` is `crit` ("HTTP 301"). Point `url` at the final address. This also keeps
  credentials from travelling to a host the config doesn't name.
- Credentials in the `url` itself are a config error, as for databases.

### `tls_expiry`

```toml
[[check]]
id = "tls.website"
type = "tls_expiry"
label = "Website certificate"
host = "www.example.org"     # port 443 unless `port`; `servername` if it differs from host
interval = "12h"
```

Value: days until the certificate the server presents expires (negative when expired). Default limits: amber at 21
days, red at 7. A Let's Encrypt certificate renews at 30 days left, so amber means renewal has failed for over a week.

**A certificate that isn't valid for the host, or isn't trusted, is `crit`** ("not valid for www.example.org"),
since browsers refuse it. Not reaching the server at all is `unknown`.

### `sql_age` and `sql_value`

```toml
[[check]]
id = "backup.nas"
type = "sql_age"
label = "NAS backup"
group = "backups"
interval = "15m"
database = "postgresql://monitor@db.example/backups"
password_env = "BACKUP_DB_PASSWORD"
query = "SELECT max(finished_at) FROM runs WHERE ok"
warn = "26h"
crit = "50h"

[[check]]
id = "queue.mail"
type = "sql_value"
label = "Mail queue"
database = "sqlite:///var/lib/mailer/queue.db"
query = "SELECT count(*) FROM outgoing WHERE sent_at IS NULL"
warn = 20
crit = 200
```

`database` is a URL: `sqlite:///path`, `mysql://` / `mariadb://`, `postgresql://`. **A password in the URL is a config
error**: use `password_env`. SQLite opens read-only (`mode=ro`). A MariaDB or PostgreSQL URL without its extra
installed is a config error that names the extra to install.

- The query must return one row with one column, the first row is used. More columns is a config-time error if
  it can be detected, otherwise a failure.
- `sql_age`: the column is a timestamp (a database time type, an RFC 3339 string or Unix seconds). A time without an
  offset is UTC unless `timezone` says otherwise. **This is the classic trap:** MariaDB `DATETIME` and SQLite text
  are usually local time, so the docs show `timezone` prominently.
- **No row or `NULL` is `crit`** ("no rows"); `empty = "ok"`, `"warn"` or `"unknown"` changes that.
- `sql_age` placeholders: `{newest}` (the timestamp itself, local time).
- Connections are opened per run and closed again. A connection that fails is `unknown`.

### `file_age`

```toml
[[check]]
id = "backup.offsite"
type = "file_age"
label = "Offsite copy"
group = "backups"
path = "/mnt/offsite/backups/*.tar.zst"   # a glob: the newest match counts
warn = "8d"
crit = "15d"
min_size = "1MB"                           # optional: smaller files don't count (a failed run leaves a stub)
```

Value: age of the newest matching file (modification time). **No matching file is `crit`** ("no file matches"). A
mount that isn't there is the same thing seen from here. Placeholders: `{file}` (the newest file's name).

### `disk`

```toml
[[check]]
id = "disk.nas"
type = "disk"
label = "NAS disk"
path = "/mnt/nas"
```

Value: used %, default limits 85 and 95. `measure = "free"` reports free space instead (in GB, worse below, no
default limits). In a container, the path is a mounted volume, and the numbers are those of the filesystem behind
it.

### `exec`

```toml
[[check]]
id = "smart.sda"
type = "exec"
label = "Disk sda health"
command = ["/usr/lib/nagios/plugins/check_ide_smart", "-d", "/dev/sda"]
interval = "1h"
```

Runs a command **without a shell** (a list, never a string), so no quoting or injection problems. Nagios plugin
conventions:

- Exit code 0, 1, 2 or 3 gives `ok`, `warn`, `crit` or `unknown`. Any other exit code is a failure.
- The first line of the output, up to `|`, is the summary.
- The first performance value after `|` (`time=0.12s;1;2`) is the value and unit. The plugin's own thresholds win:
  `warn`, `crit` and `worse` are a config error here.

On a timeout the process is killed, which a thread can't be. Environment variables are passed only when listed:
`env = ["PATH", "SMART_TOKEN"]`; nothing else, `PATH` included (on Windows most programs also need `SYSTEMROOT`).
What the command writes to stderr goes to the debug log, masked, and never into a summary.

### `docker_status`

```toml
[[check]]
id = "docker.containers"
type = "docker_status"
label = "Containers"
group = "docker"
docker = "unix:///var/run/docker.sock"    # or "tcp://socket-proxy:2375"
containers = ["db", "web", "backup"]      # by name; or `labels = ["status.watch=true"]`; default: all running ones
restarts = 3                              # more restarts than this within `restart_window` (default 1h) is amber
```

Reads the Docker API (a few `GET`s, plain HTTP over the socket, no Docker SDK). Per container:

- A listed container that is missing or not running is `crit`.
- A container that is `unhealthy` is `crit`. One that is still `starting` counts as fine; a `show_after` for `crit`
  absorbs a slow start.
- Restarting more often than `restarts` within the window is `warn`. A container that keeps restarting does serve
  in between, but something is wrong with it.

The value is the number of containers that aren't fine. The summary names them ("backup: unhealthy; web: 5 restarts
in 1 h"), and the level is the worst of them. One check covers a set of containers, so ten containers don't make
ten cards. Make separate checks if they should.

**Read-only access** (decided, see design.md): use [docker-socket-proxy] with only `CONTAINERS=1` and `IMAGES=1`, which
refuses every `POST`, rather than mounting the socket. Mounting `/var/run/docker.sock:ro` does *not* make the API
read-only, and the checker README says so plainly. The checks only ever send `GET`, without an API version in the
path (the engine answers in its own current one). On Windows there are no unix sockets to reach: `docker` must be a
`tcp://` address (the proxy).

[docker-socket-proxy]: https://github.com/Tecnativa/docker-socket-proxy

### `docker_updates`

```toml
[[check]]
id = "docker.updates"
type = "docker_updates"
label = "Image updates"
group = "docker"
interval = "1d"
show_after = { unknown = "3d" }   # a registry that is down for a day or two doesn't turn the card grey
versions = "minor"            # newer tags within the same major (default); "major" includes major bumps; "none"
ignore = ["postgres"]         # images to skip
```

Per image of a running container:

- **Rebuilt upstream:** the tag's registry digest differs from the running image's (`HEAD` on the manifest, which
  Docker Hub doesn't count as a pull).
- **Newer version:** the registry has a higher version tag of the same form (`1.27.3-alpine` → `1.27.4-alpine`,
  not → `1.28.0`). Only tags that read as versions count; `latest` is covered by the digest.
- **An image built locally: its base instead.** Docker records no name for the base (no label, and the history
  doesn't name it), but an image's layers start with its base's. So the base is the local image, pulled from a
  registry and tagged, whose `RootFS.Layers` are the longest prefix of ours (equal counts: a `FROM` that adds no
  layer). Its tag is asked about as above: "myapp (base python:3.14-slim rebuilt, rebuild)", "myapp (base python
  3.14 → 3.15)". A base that is no longer in the image store (`docker image prune`) can't be found: the summary says
  "myapp: base image not found (pruned?)" and the image doesn't count, rather than being `unknown`.
- **Built locally or pulled?** Docker 29's image inspect says so: `Identity` is `{"Pull": [{"Repository": ...}]}`
  for a pulled image and `{"Build": [...]}` for a built one; an image tagged locally under another name counts as
  built (its tag isn't in the registry it came from). Older engines have no `Identity`: there an image with a
  `RepoDigest` counts as pulled. That is right for Docker's classic image store, but with the containerd image store
  (the default of new installs) a built image has a RepoDigest too (`myapp@sha256:…`) and would still be asked
  about by its own name ("unknown (registry did not answer)").

The value is the number of images with an update. It is **`warn` at most, never `crit`**: an update is "do it this
weekend". `image_vulns` is what turns red when an update fixes something serious. The summary names the images, for
example "3 images: nginx (rebuilt), grafana 11.2 → 11.3, …". Private registries take credentials from the
environment (`registry_auth_env`). A registry that doesn't answer makes that image unknown, not the whole check:
the summary mentions it, and the level is `unknown` only if no registry answered.

### `image_vulns`

```toml
[[check]]
id = "docker.vulns"
type = "image_vulns"
label = "Vulnerabilities"
group = "docker"
interval = "1d"
timeout = "15m"             # the first run downloads Trivy's database
trivy = "trivy"             # the command, or a path

[check.schedule.update]     # per class and severity: when a CVE turns the card amber and red (the README explains each line)
CRITICAL = { warn = "0d", crit = "3d" }
[check.schedule.no_fix]
CRITICAL = { warn = "never", crit = "0d" }
```

Runs `trivy image --format json --quiet --scanners vuln <image>` **once for each image of the running containers**,
without `--ignore-unfixed`: every vulnerability is seen, and what the user can do about it decides what it means.

**Classes**, per vulnerability (CVE and package) in an image:

| Class | When | Card text |
|---|---|---|
| `update` | the image was pulled, its tag has a newer build in the registry, and that build doesn't have it | "mariadb: 1 critical, fixed, please update image" |
| `rebuild` | the image is built locally, and either its base's tag has a newer build without it, or the package didn't come with the base (Trivy's `Layer.DiffID` is not one of the base's layers: your own Dockerfile installed it), or the base can't be found | "myapp: 2 critical, fixed, please rebuild image" |
| `waiting` | a fixed version exists, but the newer build still has it, or there is no newer build (also: an image started by id or pinned by digest, and a registry that didn't answer) | "mariadb: 1 critical, waiting for upstream fix" |
| `no_fix` | Trivy gives no fixed version (`Status` affected, will_not_fix, fix_deferred, end_of_life) | "mariadb: 1 critical, no fix exists" |

"Upstream" was weighed against plain "waiting for fix": for those in the know it says more. The image's name comes
first, so the text doesn't repeat it. Pulled or built locally, and the base of a built image, are found as
`docker_updates` finds them.

**Scans:** still one per image. An extra scan only for a tag that has a newer build: the image's own tag (pulled)
or its base's (built locally, and only when a fixable CVE came with the base), straight from the registry
(`trivy image --image-src remote <tag>`), each tag at most once per run. Whether a tag has a newer build is one
manifest `HEAD`, as in `docker_updates` (with its `registry_auth_env`). No second check scans everything again, and
no CVE is reported twice.

**The schedule** (`[check.schedule.<class>]`, `SEVERITY = { warn = "...", crit = "..." }`): per class and
severity, a `warn` and a `crit` time with the rules of `show_after` (`s`, `m`, `h`, `d`, `"0d"`, `"never"`), per
CVE instead of per card, counted from **when the CVE was first found in that image** (by the image reference the
container uses, such as `mariadb:11.4`, so pulling a newer build doesn't reset it). So: ignore it (leave the
severity out), amber after *x* and red after *y*, or straight to red after *z* (`warn = "never"`). Both times are
written out: a missing one is a config error, because skipping a level should be visible to whoever reads the
config. A class or severity left out doesn't count (the README's example says so). Without a `schedule`: a fixed
`CRITICAL` is red and a fixed `HIGH` amber at once, in every class, and `no_fix` doesn't count, which is what the
check did before classes existed.

- **First found** is kept per image reference and CVE in the check's memory (`extra`, under the
  [continuity rule](#memory-the-sqlite-file)). A new critical doesn't inherit the age of an old one. A CVE that is no
  longer in a scanned image is forgotten; if it comes back, it is new. An image that couldn't be scanned keeps its
  dates. The age goes on when a CVE changes class (it was found when it was found).
- The card's level is the worst over all CVEs. Its summary names the worst: of the CVEs at that level, the highest
  severity, then the image and class with the most such CVEs (ties: `update`, `rebuild`, `waiting`, `no_fix`).
  Cards are narrow, so that is all it says, plus "; 1 not scanned (…)" when an image couldn't be scanned (a blind
  spot). The value is the number of CVEs that are due (amber or red). Nothing due: "nothing due in 5 images".
- **The full list** is on the checker's page [`/vulns`](#serving).
- **Known to be exploited (CISA's KEV list):** Trivy's report doesn't say (checked on Trivy 0.76.2's JSON: no such
  field; only descriptions mention exploits). Trivy's maintainers have open requests for it (discussions #10706,
  #10840). Nothing is built on it.
- Trivy not being installed is a config error at startup. Its cache (`--cache-dir`) should be on a volume, so the
  database isn't downloaded every day.

### `python`

```toml
[[check]]
id = "solar.today"
type = "python"
label = "Solar yield"
function = "my_checks:solar_today"    # module:function
path = "/etc/anystatus/checks"        # optional: where to import from
site = "roof"                         # any other key is passed to the function
warn = 2
crit = 0.5
worse = "below"
```

The function gets the check's config table (a `dict`) and returns either a number, which is compared with the
limits, or `anystatus.Observation(level=..., summary=..., value=..., unit=...)` when it decides the level itself.
It may also be `async`. An exception is a failure; its type, not its message, goes into the summary (message text can
contain secrets). It runs in a worker thread with the usual [timeout rules](#scheduling).

### `result_file`

```toml
[[check]]
id = "jobs.nightly"
type = "result_file"
path = "/var/lib/nightly/status.json"
```

Reads a file that another program writes in the [SPEC.md](../SPEC.md) format: a single result, or a document whose
results are all taken over. **Any language can report this way, without a push endpoint**, and it is the "write a
result by hand" example from design.md.

The file's `ts` and `ttl` are passed on unchanged, so a job that stops writing still turns grey on time, and the
checker doesn't make it look fresh. The file's level isn't held back by `show_after`, because the job decided it. Ids in a document
are prefixed with this check's id (`jobs.nightly/<id>`) so they can't clash. A result without a `label` is named by
its own id (in a document) or by the check's `label` (a single result). `warn`, `crit`, `worse` and `summary` are a
config error here.

**Reading the file can fail** (missing, unreadable, not JSON, over 1 MB, an unsupported spec): that is `unknown`
under the check's own id, **held back by `show_after.unknown`** like any failure, so a job that rewrites its file in
place doesn't make the display blink. Until then, the last results stay served as they were (they carry their own
`ts`, so they still age out on time). Once it shows, a single result is replaced by it; a document's
results stay next to it, so a red result can't vanish behind one grey "file not found". A result in the file that
breaks SPEC.md is served as `unknown` ("invalid in file: no ttl") under its id. Better still, the job writes to a
temporary file and renames it.

### `systemd` (proposed, later)

```toml
[[check]]
id = "backup.timer"
type = "systemd"
unit = "restic-backup.service"
max_age = "26h"      # optional: for a unit run by a timer, how long ago it last finished successfully
```

A failed unit is `crit`. With `max_age`, a unit that last succeeded longer ago than that is `crit` too. That is
"check the result where it lives" for the many backups that run as systemd timers. Needs `systemctl` on the host,
so not from a container without extra setup.

### `dns` (proposed, later)

Resolves a name and optionally compares the answer (`expect = ["192.0.2.10"]`); the value is the lookup time. The
standard library can only ask the system's resolver, so asking a *specific* server (is the Pi-hole answering?) needs
an extra (`dnspython`).

### `mqtt` (step 4)

MQTT as a **source**: a device or service that publishes a heartbeat or a state. (design.md keeps MQTT out as the
*transport* to the display; that is a different question.)

```toml
[mqtt.home]                         # a broker; every mqtt check names one, and they share its connection
host = "broker.example"
port = 8883                         # 1883 without TLS
tls = true
username = "anystatus"
password_env = "MQTT_PASSWORD"

[[check]]
id = "boiler.heartbeat"
type = "mqtt"
label = "Boiler controller"
broker = "home"
topic = "boiler/heartbeat"
interval = "30s"                    # how often the age is evaluated, not how often the device sends
warn = "3m"
crit = "10m"

[[check]]
id = "freezer.temp"
type = "mqtt"
label = "Freezer"
broker = "home"
topic = "freezer/state"
json = "temperature"                # a value from the payload, compared with the limits
warn = -15
crit = -10
max_age = "15m"                     # no message for this long is `crit` ("silent for 20 min")

[[check]]
id = "plug.status"
type = "mqtt"
label = "Garage plug"
broker = "home"
topic = "plug/garage/LWT"
levels = { Online = "ok", Offline = "crit" }   # the payload names the level (Last Will messages)
```

Three ways to read a topic:

- **Heartbeat** (no `json`, no `levels`): the value is the age of the last message. "Is it still alive?"
- **Value** (`json` or a plain numeric payload): the value comes from the last message, with the usual limits.
  `max_age` adds the heartbeat rule on top.
- **Level** (`levels`): the payload maps to a level. A payload not in the table is `unknown` ("unexpected payload
  'Rebooting'").

Rules:

- **Retained messages don't count as a heartbeat.** At subscribe, a broker hands over the last retained message at
  once, with no time on it. Counting it would make a dead device look alive on every checker restart (the trap
  design.md warns about). A retained message *is* used for its content (the payload, the level), and for its time
  only if the payload carries one (`time_json = "ts"`).
- **The last message's time and payload are kept in the SQLite memory.** A restart then doesn't reset a heartbeat's
  age to zero or forget a state. They are kept only within the [continuity rule](#memory-the-sqlite-file).
- **No message ever seen** (none since the checker started, none restored from the memory): the age is at least the
  time since the latest (re)connect to the broker. Earlier time doesn't count: a message sent while the broker was
  out of reach may have been missed. If that lower bound already reaches `crit`, the result is `crit` ("no
  message for over 10 min"); if it reaches `warn`, `warn`; otherwise `unknown` ("no message yet").
- **A broker the checker can't reach** makes its checks `unknown` ("broker home unreachable"), held back by
  `show_after.unknown` like any failure. Reconnecting is automatic, with backoff.
- `ts` is when the check was evaluated, as for every check. The message time feeds the value, never `ts`.
- Needs the `mqtt` extra (paho-mqtt), since a subscribe client isn't in the standard library. It is a new kind of
  check: a listener that runs all the time next to periodic evaluation. The step 3 structure has to leave room for
  that.
- **Later:** wildcard topics (`sensors/+/heartbeat`), where the value is the number of silent devices and the
  summary names them.

### `system`

```toml
[[check]]
id = "pi.temp"
type = "system"
label = "Pi temperature"
measure = "temperature"   # or "load" (1-min load per CPU), "memory" (used %), "uptime"
warn = 70
crit = 80
```

Linux only, read from `/proc` and `/sys`, standard library. Covers the questions everyone asks of their own Pi
without needing Nagios plugins. In a container it reports the container's view (the host's temperature and load,
the container's memory limit if one is set). The docs say so.

### Through `exec` rather than a type of their own

SMART disk health, RAID state, UPS: these need root or vendor tools, and good Nagios plugins exist for them
(`check_smart`, `check_md_raid`, `check_nut`).
