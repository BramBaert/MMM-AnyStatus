# MMM-AnyStatus checker

The Python service behind [MMM-AnyStatus](../README.md): it runs checks on their intervals, decides each result's level, remembers
when it changed, and serves the results as JSON (the format in [SPEC.md](../SPEC.md)) on `GET /results`. How it works,
and every rule it follows: [docs/checker.md](../docs/checker.md).

Python 3.11 or newer, standard library only in the core. Database drivers are extras.

## Quick start (5 minutes, with [uv](https://docs.astral.sh/uv/))

```sh
cd checker
uv sync                                          # installs the checker and its dev tools into .venv
uv run examples/update_demo_db.py                # creates examples/demo.sqlite with a fresh heartbeat
uv run anystatus-checker validate examples/config.toml
uv run anystatus-checker once examples/config.toml    # run every check once, print the document
uv run anystatus-checker run examples/config.toml     # the service; stop with Ctrl+C
```

Then open <http://127.0.0.1:8099/> (a readable overview) or fetch <http://127.0.0.1:8099/results> (the JSON). The
example config has four checks:

| Check | What it shows |
|---|---|
| `ping.localhost` | the `ping` type, with limits in ms |
| `demo.heartbeat` | `sql_age` on a SQLite file: green while `uv run examples/update_demo_db.py --loop` keeps writing, amber after a minute without, red after five |
| `demo.nightly` | `sql_age` on data that is 3 days old on purpose: red. The time has no offset, so `timezone` says which clock wrote it |
| `custom.disk` | the `python` type: your function (`examples/my_checks.py`) returns a number |

Point the MagicMirror module at it: `checkerUrl: "http://127.0.0.1:8099"`.

To use it in your own project: `uv add mmm-anystatus-checker` (not on PyPI yet: `uv add ./checker` from a checkout),
with `uv add "mmm-anystatus-checker[postgres]"` or `[mysql]` for those databases, `[mqtt]` for MQTT.

## Commands

| Command | |
|---|---|
| `anystatus-checker run config.toml` | the service. `-v` logs every run |
| `anystatus-checker validate config.toml` | checks the config and exits; lists *all* problems |
| `anystatus-checker once config.toml [--id ID]` | runs the checks once and prints the document (no memory file is touched) |

## Check types in this version

- **`ping`**: `host`, `count` (default 3). The system `ping`, no root. No answer is `crit`.
- **`sql_age`**: `database` (`sqlite:file.db`, `sqlite:///abs/path.db`, `mysql://user@host/db`, `postgresql://user@host/db`),
  `query` (one row, one column: a timestamp), `password_env`, `timezone`, `empty`. A password in the URL is refused.
  SQLite opens read-only. **The classic trap:** a time without an offset is UTC unless you set `timezone`; MariaDB
  `DATETIME` and many SQLite columns hold local time.
- **`python`**: `function = "module:function"`, optional `path`; the function gets the other keys as a dict and returns
  a number or `anystatus.Observation(level=..., summary=..., value=..., unit=...)`. May be `async`.

- **`mqtt`**: a device that publishes a heartbeat or a state. Needs the `mqtt` extra
  (`uv add "mmm-anystatus-checker[mqtt]"`, or `uv sync --extra mqtt` in a checkout). Brokers are a table, shared by
  the checks that name them (one connection per broker, automatic reconnect):

  ```toml
  [mqtt.home]
  host = "broker.example"
  port = 8883            # default 1883, or 8883 with tls = true
  tls = true             # optional ca_file = "ca.pem" (relative to this file) for a private CA
  username = "anystatus"
  password_env = "MQTT_PASSWORD"
  # client_id = "..."    # optional

  [[check]]
  id = "boiler.heartbeat"
  type = "mqtt"
  broker = "home"
  topic = "boiler/heartbeat"     # one exact topic: wildcards (+, #) are not supported yet
  warn = "3m"
  crit = "10m"
  ```

  Three ways to read a topic. **Heartbeat** (no `json`, no `levels`): the value is the age of the last message, the
  limits are durations. **Value** (`json = "a.b.0.c"`, or a plain numeric payload together with `max_age`): the
  value comes from the last message, with the usual limits; `max_age` makes a topic that has been silent that long
  `crit`. **Levels** (`levels = { Online = "ok", Offline = "crit" }`): the payload names the level; any other
  payload is `unknown`. `json` and `levels` cannot be combined.

  **Retained messages never count as a heartbeat** (a restart would make a dead device look alive): their payload
  is used, their time only if the payload carries one (`time_json = "ts"`, RFC 3339 or Unix seconds). The last
  message's time and payload are kept in the memory file, so a restart does not reset the age. A broker that
  cannot be reached makes its checks `unknown` ("broker home unreachable"). If no message was ever seen, the age is
  at least the time since the checker subscribed: it turns `warn` or `crit` once that reaches a limit, and is
  `unknown` ("no message yet") before. `once` exits right after starting, so it reports the broker as unreachable
  for `mqtt` checks: use `run`.

Every check also takes `label`, `group`, `interval`, `timeout`, `ttl`, `confirm`, `warn`, `crit`, `worse` and `summary`
(a template: `"last backup {age} ago"`), as described in docs/checker.md.

Secrets never sit in the file: a key ending in `_env` names an environment variable (`password_env = "DB_PASSWORD"`).

## Development

```sh
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy src
uv run --python 3.11 pytest        # the oldest supported Python
```

The real-database tests (MariaDB, PostgreSQL) are skipped: they need a server and the extras
(`uv sync --extra mysql --extra postgres`). The mqtt test against a real broker is skipped unless
`ANYSTATUS_TEST_MQTT=host:port` names one (`docker run --rm -p 1883:1883 -v ${PWD}/tests/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro eclipse-mosquitto:2.0.22`).

A new check type is one file in `src/anystatus/checks/` (subclass `Check`, decorate with `@register`, import it in
`checks/__init__.py`). A type can also listen all the time (`start`/`stop`), keep its own data in the memory file
(`CheckMemory`), and own a top-level config section (`@register_section`).
