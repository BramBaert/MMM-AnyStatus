# MMM-AnyStatus checker

The Python service behind [MMM-AnyStatus](../README.md): it runs checks on their intervals, decides each result's level, remembers
when it changed, and serves the results as JSON (the format in [SPEC.md](../SPEC.md)) on `GET /results`. How it works,
and every rule it follows: [docs/checker.md](../docs/checker.md).

Python 3.11 or newer, standard library only in the core. Database drivers are extras.

## Quick start (5 minutes, with [uv](https://docs.astral.sh/uv/))

```sh
cd checker
uv sync                                          # installs the checker and its dev tools into .venv
uv run examples/update_demo_db.py                # creates examples/demo.sqlite and demo_result.json, fresh (add --loop to keep them so)
uv run anystatus-checker validate examples/config.toml
uv run anystatus-checker once examples/config.toml    # run every check once, print the document
uv run anystatus-checker run examples/config.toml     # the service; stop with Ctrl+C
```

Then open <http://127.0.0.1:8099/> (a readable overview) or fetch <http://127.0.0.1:8099/results> (the JSON). The
example config has eight checks:

| Check | What it shows |
|---|---|
| `ping.localhost` | the `ping` type, with limits in ms |
| `demo.heartbeat` | `sql_age` on a SQLite file: green while `uv run examples/update_demo_db.py --loop` keeps writing, amber after a minute without, red after five |
| `demo.nightly` | `sql_age` on data that is 3 days old on purpose: red. The time has no offset, so `timezone` says which clock wrote it |
| `custom.disk` | the `python` type: your function (`examples/my_checks.py`) returns a number |
| `http.self` | the `http` type, asking this checker's own `/results` (green under `run`; `once` serves nothing, so it says "connection refused" there) |
| `disk.examples` | the `disk` type on the examples folder |
| `file.demo` | `file_age` on `examples/demo.sqlite`: green while the loop writes, then amber, then red |
| `demo.job` | `result_file`: reads `examples/demo_result.json`, which the same script rewrites; stop it and the card turns grey after 90 s |

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
- **`sql_value`**: a number from a query. The same `database`, `password_env` and `empty` as `sql_age` (and the same
  read-only connection), but no `timezone`. Higher is worse unless `worse = "below"`.

  ```toml
  [[check]]
  id = "queue.mail"
  type = "sql_value"
  label = "Mail queue"
  database = "sqlite:///var/lib/mailer/queue.db"
  query = "SELECT count(*) FROM outgoing WHERE sent_at IS NULL"
  warn = 20
  crit = 200
  ```

- **`tcp`**: `host`, `port`. The value is the time to connect, in ms. A refused port, or no answer within the
  timeout, is `crit` ("port 3306 refused"); a name that doesn't resolve is `unknown`.

  ```toml
  [[check]]
  id = "tcp.mariadb"
  type = "tcp"
  label = "MariaDB port"
  host = "db.example"
  port = 3306
  ```

- **`http`**: one GET. Without `json` the value is the response time in ms; an unexpected status is `crit`
  ("HTTP 503"), a connection that fails is `unknown`. With `json = "a.b.0.c"` the value comes from the JSON answer: a
  number, or with `value_is = "time"` a timestamp (RFC 3339 or Unix seconds; no offset means UTC unless `timezone`)
  whose age is compared with the limits. A path that isn't there, or a value that is no number or time, is `unknown`.
  `expect_status` defaults to any 2xx. `username` with `password_env` is basic auth; `bearer_env` sends a bearer token.
  In the `headers` table a key ending in `_env` names the environment variable that holds the value
  (`X-Api-Key_env = "SYNC_KEY"`). TLS is verified; `verify_tls = false` is allowed and logged as a warning at startup.
  Redirects are not followed (a 301 is "HTTP 301"), at most 1 MB of the answer is read, and neither the URL's query,
  the credentials, the headers nor any body text ever appear in a summary.

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
  value_is = "time"                        # "number" (default) or "time"
  warn = "2h"
  crit = "1d"
  ```

- **`tls_expiry`**: `host`, `port` (443), `servername` (if it differs from `host`). The value is the days until the
  certificate the server presents expires (negative once it has); amber at 21 days and red at 7 unless you set
  limits. A certificate that isn't trusted or isn't valid for the name is `crit` ("not valid for www.example.org",
  "not trusted (self-signed certificate)"), as is an expired one; a server that can't be reached is `unknown`.

  ```toml
  [[check]]
  id = "tls.website"
  type = "tls_expiry"
  label = "Website certificate"
  host = "www.example.org"
  interval = "12h"
  ```
- **`python`**: `function = "module:function"`, optional `path`; the function gets the other keys as a dict and returns
  a number or `anystatus.Observation(level=..., summary=..., value=..., unit=...)`. May be `async`.

- **`file_age`**: the age of the newest file that matches the glob `path` (`**` works). No match is `crit` ("no file
  matches"), also when the folder is gone (a mount that isn't there); a folder that exists but can't be read is
  `unknown`. `min_size` ignores smaller files (a failed run leaves a stub): plain bytes or decimal units, `"500KB"`,
  `"1MB"`, `"2GB"` (KB = 1000 bytes). Limits are durations. Placeholder `{file}` is the newest file's name. A relative
  `path` is relative to the config file.

  ```toml
  [[check]]
  id = "backup.offsite"
  type = "file_age"
  label = "Offsite copy"
  path = "/mnt/offsite/backups/*.tar.zst"
  min_size = "1MB"
  warn = "8d"
  crit = "15d"
  ```

- **`disk`**: used % of the filesystem behind `path`, worse above, amber at 85 and red at 95 unless you set limits. The
  percentage is what `df` shows (of what a normal user can use). `measure = "free"` reports the free space in GB
  instead: worse below, no default limits. A path that isn't there is `unknown`. In a container the path is a mounted
  volume and the numbers are those of the filesystem behind it. Placeholder `{free}` (GB).

  ```toml
  [[check]]
  id = "disk.nas"
  type = "disk"
  label = "NAS disk"
  path = "/mnt/nas"

  [[check]]
  id = "disk.nas.free"
  type = "disk"
  path = "/mnt/nas"
  measure = "free"
  warn = 100              # GB
  crit = 20
  ```

- **`exec`**: runs `command`, a list of strings **without a shell** (a string is a config error), as a Nagios plugin: exit
  0, 1, 2, 3 are `ok`, `warn`, `crit`, `unknown`, any other exit code is `unknown` ("exit code 4"). The first output
  line up to `|` is the summary; the first performance value after it (`time=0.12s;1;2`) is the value and unit. The
  plugin's own thresholds win, so `warn` and `crit` are a config error. A run longer than `timeout` is killed (with
  its process group where the OS allows) and is `unknown` ("timed out after 10 s"). **Only the environment variables
  listed in `env` are passed on, and only if they are set: `PATH` too** (give the command as an absolute path; on
  Windows list `SYSTEMROOT` if the program needs it). At most 64 KB of the output is read; stderr goes to the debug
  log only (`-v`), with the listed variables' values masked, never into the summary.

  ```toml
  [[check]]
  id = "smart.sda"
  type = "exec"
  label = "Disk sda health"
  command = ["/usr/lib/nagios/plugins/check_ide_smart", "-d", "/dev/sda"]
  env = ["SMART_TOKEN"]
  interval = "1h"
  ```

- **`system`**: Linux only, read from `/proc` and `/sys`. `measure` is `temperature` (°C, the highest thermal zone, or
  `zone = "thermal_zone0"`), `load` (1-minute load average divided by the number of CPUs), `memory` (used % from
  MemTotal and MemAvailable; **if a cgroup v2 memory limit (`memory.max`) below the machine's memory applies, that
  limit is used instead**, as in a container with `mem_limit`) or `uptime` (a duration; limits such as `"30d"`, use
  `worse = "below"` to catch a recent reboot). On another system, or without the sensor, it is `unknown` ("system
  checks only work on Linux", "no temperature sensor"), not a config error, so the same config still loads on a
  laptop. In a container it reports the host's temperature and load, but its own memory limit.

  ```toml
  [[check]]
  id = "pi.temp"
  type = "system"
  label = "Pi temperature"
  measure = "temperature"
  warn = 70
  crit = 80
  ```

- **`result_file`**: reads the file `path`, in which another program (any language) writes a
  [SPEC](../SPEC.md) result, or a whole document, and serves what it says. The file's `level`, `summary`, `value`,
  `unit`, `since`, `ts` and `ttl` are passed on as they are: **not held back by `show_after` and not re-stamped**, so a job that stops
  writing turns grey on time. A single result is served under the check's `id`; the results of a document under
  `<check id>/<their id>` (at most 128 characters in all). `label` and `group` default to the check's. A result that is
  invalid (no `ts` or `ttl`, a time without an offset, a bad level, an id used twice, ...) is shown as `unknown` with
  "invalid in file: <reason>", `ts` = when the file was read, `ttl` = the check's. A missing or unreadable file, one
  that is not JSON, larger than 1 MB, or with an unsupported `spec` (`2.0`) is one `unknown` result under the
  check's `id`, which does wait for `show_after.unknown`. `warn`, `crit`, `worse` and `summary` are a config error. Have the
  writer rename a finished file into place, so nobody reads half of it.

  ```toml
  [[check]]
  id = "jobs.nightly"
  type = "result_file"
  label = "Nightly jobs"
  path = "/var/lib/nightly/status.json"
  ```

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

- **`docker_status`**, **`docker_updates`**, **`image_vulns`**: the Docker checks. They talk to the Docker API with
  `GET` requests only (standard library, no Docker SDK), at `docker = "unix:///var/run/docker.sock"` (the default) or
  `docker = "tcp://host:2375"`. Paths carry no API version: the engine answers in its own current one. **On Windows
  only `tcp://` works** (a `unix://` address is a config error): reach Docker through a socket proxy. See
  [Read-only access to Docker](#read-only-access-to-docker) first.

  **`docker_status`**: are the containers running, healthy and not restarting in a loop? `containers = ["db",
  "web"]` (by name), or `labels = ["status.watch=true"]` (all of them must match), or neither: every running
  container. Not both. A *listed* container that is missing or not running is `crit` ("backup: missing", "db: not
  running (exited)"; with `labels` or the default only running containers are looked at). `unhealthy` is `crit`,
  `starting` is fine. More than `restarts` (default 3) restarts within `restart_window` (default `"1h"`) is `warn`
  ("web: 5 restarts in 1 h"): the growth of Docker's `RestartCount`, kept in the memory file so the window survives a
  checker restart (within the `ttl`). The first sighting of a container never counts as growth. The value is the number of
  containers that are not fine, the level the worst. The check decides its own level: `warn`, `crit` and `worse` are
  a config error. Docker unreachable (or a proxy that refuses the path) is `unknown`.

  **`docker_updates`**: does an image of a running container have an update in its registry? *Rebuilt*: the digest
  the registry gives for the tag (one `HEAD` on the manifest, which Docker Hub doesn't count as a pull) is none of
  the local image's `RepoDigests` (multi-arch images: the index digest on both sides). *Newer version*: a higher tag
  with the same shape (`1.27.3-alpine` → `1.27.4-alpine`; not `1.27.4`, not `1.27.4-perl`): `versions = "minor"`
  (default) allows the same major, `"major"` any, `"none"` only the digest. `latest` and other tags that aren't
  versions are checked by digest only. An image started by digest (`image@sha256:...`) is skipped on purpose (the
  summary says "1 pinned by digest, skipped"). **An image built locally is checked through its base image**: Docker
  doesn't record the base's name, but an image's layers start with its base's, so the base is the local registry
  image whose layers are the longest prefix of its own. The summary then says "myapp (base python:3.14-slim rebuilt,
  rebuild)" or "myapp (base python 3.14 → 3.15)". If the base is no longer in the image store (`docker image prune`),
  it says "myapp: base image not found (pruned?)" and the image doesn't count. Whether an image was built locally
  comes from Docker 29's `Identity` field; older engines don't have it, and there an image with a `RepoDigest` counts
  as pulled, which is right for the classic image store but not for the containerd one (where a built image has a
  RepoDigest too). The value is the number
  of images with an update, **`warn` at most** (`crit` is a config error; `warn = 2` makes it amber from two images);
  the summary names them: "3 images: nginx (rebuilt), grafana 11.2 → 11.3, …". `ignore = ["postgres",
  "ghcr.io/org/app:1"]` skips images (by name, repository, or name:tag). A registry that doesn't answer makes
  *that image* unknown (named in the summary); the check is `unknown` only if no registry answered. Tag lists are read
  in pages (at most 10 of up to 1000 tags, then the summary says "tag list cut after 10 pages"). Registries are asked
  over https; `localhost` and `127.x` over http (as Docker does). Private registries:
  `registry_auth_env = { "ghcr.io" = "GHCR_AUTH" }` maps a registry host to an environment variable that holds
  **`user:password`** (for ghcr.io a personal access token as the password). It is sent only to that registry's own
  token service (or Docker Hub's), never to another host a registry names. The usual interval is a day.

  **`image_vulns`**: runs [Trivy](https://trivy.dev) (`trivy image --format json --ignore-unfixed --quiet --scanners
  vuln <image>`, a list, no shell) for each distinct image and counts **only vulnerabilities that have a fix**. A
  fixable `CRITICAL` is `crit`, any other counted severity (`HIGH` by default) is `warn`; `severities = ["CRITICAL",
  "HIGH", "MEDIUM"]` changes which severities count at all. The value is the number of fixable CVEs, the summary
  names the worst image ("grafana: 2 critical, 1 high, fixed in 11.3.1"; "fixed in" appears when one version fixes
  the worst ones). `trivy = "trivy"` is the command, a path, or a list such as `["docker", "run", "--rm", ...]`; **if it
  can't be found the config is invalid** (and a Trivy that vanishes later makes the check `unknown`). `cache_dir`
  is Trivy's `--cache-dir`: keep it on a volume so the database isn't downloaded every day. **`timeout = "15m"`**
  (the first run downloads the database; the default 10 s is too short, and the config warns about that): each
  image's scan gets the time that is left of the run, and a timeout kills Trivy. An image that couldn't be scanned
  is counted in the summary ("1 not scanned"); no image scanned at all is `unknown`. As for `exec`, only the
  environment variables listed in `env` reach Trivy, `PATH` included: Trivy usually needs `env = ["HOME",
  "DOCKER_HOST"]` (it reads local images through the Docker API, so `DOCKER_HOST=tcp://docker-socket-proxy:2375`
  with `IMAGES=1` on the proxy; otherwise it pulls from the registry). The checker image doesn't contain Trivy.
  `warn`, `crit` and `worse` are a config error.

  ```toml
  [[check]]
  id = "docker.containers"
  type = "docker_status"
  label = "Containers"
  docker = "tcp://docker-socket-proxy:2375"
  containers = ["db", "web", "backup"]
  restarts = 3

  [[check]]
  id = "docker.updates"
  type = "docker_updates"
  label = "Image updates"
  docker = "tcp://docker-socket-proxy:2375"
  interval = "1d"
  show_after = { unknown = "3d" }                   # a registry down for a day doesn't turn the card grey
  ignore = ["postgres"]
  registry_auth_env = { "ghcr.io" = "GHCR_AUTH" }   # GHCR_AUTH=user:token

  [[check]]
  id = "docker.vulns"
  type = "image_vulns"
  label = "Vulnerabilities"
  docker = "tcp://docker-socket-proxy:2375"
  interval = "1d"
  timeout = "15m"
  trivy = "/usr/local/bin/trivy"
  cache_dir = "/var/cache/trivy"
  env = ["HOME", "DOCKER_HOST"]
  ```

### Read-only access to Docker

The Docker socket is root on its host: anything that can `POST` to it can start a privileged container. The checks
only ever `GET`, but **mounting `/var/run/docker.sock:ro` does not make the API read-only**: `:ro` protects the
socket *file* from being replaced, while every request through it still works. Put a
[docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) in between, which refuses every `POST` and every
section you don't switch on, and point the checks at it. The checker then needs no access to the socket at all:

```yaml
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy:v0.5.0
    environment:
      CONTAINERS: 1      # GET /containers/...: docker_status, docker_updates, image_vulns
      IMAGES: 1          # GET /images/...: docker_updates (and Trivy reading local images)
      POST: 0            # the default: no change is possible
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    # no ports: only the checker's network can reach it
  checker:
    # ...
    depends_on: [docker-socket-proxy]
```

and `docker = "tcp://docker-socket-proxy:2375"` in the checks. `compose.dev.yaml` in this repository runs exactly
this for the dev stack; a `POST /containers/create` through it answers `403 Forbidden`.

Every check also takes `label`, `group`, `interval`, `timeout`, `ttl`, `show_after`, `warn`, `crit`, `worse` and
`summary` (a template: `"last backup {age} ago"`), as described in docs/checker.md.

### `show_after`: how long a level must last before it shows

```toml
show_after = { warn = "14d", crit = "7d", unknown = "2m" }
```

A level shows once it has lasted its time; until then the highest lower level whose time has passed is shown (a run
that sees `crit` counts towards `warn` too). With the line above, a check that keeps seeing `crit` stays green for 7
days and then turns red: amber would only show after 14. `{ warn = "0d", crit = "7d" }` is amber at once and red
after 7 days. **Getting better always shows at once.** A failure (`unknown`) has no lower level: until its time has passed the last result stays.
Times use `s`, `m`, `h`, `d` (`"30d"` for a month); `"0d"` is at once, `"never"` skips the level. **A level without a
time shows at once** (there is no default); `[defaults]` can hold `show_after`, and a check's own table overrides it
per level. A level shows at the first run at or after its time has passed: never early, at most one `interval` late
(a time shorter than the interval means "at the next run", and the config warns about it). `confirm`, which this
replaces, is a config error. Details: [docs/checker.md](../docs/checker.md).

Secrets never sit in the file: a key ending in `_env` names an environment variable (`password_env = "DB_PASSWORD"`).

## Development

```sh
uv sync --all-extras               # the dev tools and every extra: the mqtt tests need paho-mqtt
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy src
uv run --python 3.11 --all-extras pytest   # the oldest supported Python (rebuilds .venv on 3.11)
```

The real-database tests (`tests/test_real_databases.py`: `sql_age` and `sql_value` on MariaDB and PostgreSQL) are
skipped unless `ANYSTATUS_TEST_MARIADB=host:port` / `ANYSTATUS_TEST_POSTGRES=host:port` name a server and
`ANYSTATUS_TEST_DB_PASSWORD` holds the password of user `anystatus` on database `anystatus_test`; the file's docstring
has the `docker run` commands. Run them with `uv run --extra mysql --extra postgres pytest tests/test_real_databases.py`.
The mqtt test against a real broker is skipped unless
`ANYSTATUS_TEST_MQTT=host:port` names one (`docker run --rm -p 1883:1883 -v ${PWD}/tests/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro eclipse-mosquitto:2.0.22`).
`tests/certs` holds throwaway self-signed certificates (localhost) for the `tls_expiry` and `http` tests.
A new check type is one file in `src/anystatus/checks/` (subclass `Check`, decorate with `@register`, import it in
`checks/__init__.py`). A type can also listen all the time (`start`/`stop`), keep its own data in the memory file
(`CheckMemory`), and own a top-level config section (`@register_section`).
