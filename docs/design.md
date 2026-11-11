# MMM-AnyStatus: design

What MMM-AnyStatus is meant to be, and the decisions made so far. **Decided** means agreed; **open** means still to
be settled. This is the starting point for building it: `SPEC.md`, the README and the code follow from here.

## The goal

A calm status display for MagicMirror²: a big **"All OK"** when everything is fine, otherwise **only what needs
attention**, with the reason and how long it has been like that. Fed by a background Python service that does the
checking, generic enough that a ping, a container's health or "the newest row in this table is too old" are each a
few lines of config.

Why a new module: a search of all 1,497 modules on modules.magicmirror.builders (2026-10-25) found nothing that
comes close. It aims for the look of MMM-ServerStatus, the freedom of MMM-MysqlQuery in *what* counts as a status,
and the leanness of MMM-MQTT.

## Three levels, so it isn't red all the time

- **Green (`ok`):** fine.
- **Amber (`warn`):** "do it this weekend", e.g. pending updates. Never alarming.
- **Red (`crit`):** broken now: data is being lost, a backup is missing, a fixable critical vulnerability is left
  unfixed. If red shows up for something that can wait, that check's limits are wrong.
- **Grey (`unknown`):** no recent result. The display can't tell whether things are fine.

## The two parts

```
  sources            checker (Python service)                 MagicMirror
  -------            ------------------------                 -----------
  containers  ─┐     runs each check on its interval,         module's node_helper
  databases   ─┤ ─►  applies the limits, remembers state  ◄── fetches the results
  web APIs    ─┤     in SQLite, serves the results            over HTTP; the browser
  hosts       ─┘     over HTTP (port in its config)           shows only non-green
```

1. **The checker:** a Python package and service. It runs the checks, decides each level, and serves the current
   results over HTTP.
2. **The module:** MagicMirror's side. Its `node_helper` fetches the results from the checker and the browser
   shows them.

**The checker decides the level; the module only displays it.** The module has one rule of its own, the age rule
below.

## The result format (to become `SPEC.md`)

**Decided: the format comes first**, so any language can produce results. One versioned JSON result per check:

| Field | Meaning |
|---|---|
| `id` | unique, stable name of the check |
| `label` | what the display shows |
| `group` | for collapsing many items into a count per group |
| `level` | `ok`, `warn`, `crit` or `unknown` |
| `summary` | the reason, short (e.g. "last backup 3 days ago") |
| `value`, `unit` | optional, the measured value |
| `since` | when the check got its current level |
| `ts` | when this result was made |
| `ttl` | its failing age: how long the result may be believed |

**Decided, a hard rule:** every result carries `ts`, and the module compares it with its own clock. A result older
than `ts + ttl` is shown grey ("no data since …"), whatever its level says. So a dead checker, a dead network or a
stuck check can never hide behind a last "OK".

## The checker

**Decided:**
- **Check the result where it lives.** Anything that already has a way to report (a container's health, a table's
  newest row, an API's "last run") is read by the checker. A job reports itself (push) only when it leaves no such
  trace.
- **Generic, driven by a config file in TOML.** TOML because comments can say *why* a limit is what it is, and
  Python reads it with its standard library (`tomllib`). Per check: its **type**, its **interval**, its
  **amber/red limits** and its **failing age**. Secrets (passwords, tokens) come from the environment, never from
  the config file.
- **Built-in check types** (names are a first draft):
  - `ping`, `tcp`
  - `http`: a status code, or a value from the JSON answer compared against limits; user/password from the
    environment
  - `tls_expiry`: days until a certificate expires
  - `sql_age` / `sql_value`: the age of the newest row, or a value, from a query. SQLite, MariaDB/MySQL and
    PostgreSQL; each database driver is an optional extra, so nobody installs drivers they don't use
  - `docker_status`: containers running and healthy, not restarting over and over
  - `docker_updates`: a **new digest on the same tag** (upstream rebuilt it: a patch or security fix) and **newer
    version tags** (a functional update; also catches images pinned to an exact version, which never get a new
    digest). Registry manifest HEAD requests, which Docker Hub doesn't count against its pull limit
  - `image_vulns`: wraps Trivy's JSON output and counts **only CVEs with a fix we can apply** (otherwise it's amber
    forever: base images always carry "will not fix" CVEs). Optional, needs Trivy. Meant to run **daily**
  - `file_age`, `disk`
  - `exec` with Nagios exit codes, so existing check plugins work
  - a custom Python function, as the escape hatch for anything else
- **It remembers in a SQLite file of its own**, so a restart doesn't forget that a check is halfway its failing
  age. Most ages come from the source's own timestamps (read again after a restart); the file holds what has none:
  when a level started (`since`), the last pushed facts, the debounce. In a container: a volume, so it survives
  restarts and new versions. Losing it only resets those.
- **It serves its current results over HTTP**, on a port from its config. Each result again every round (a
  heartbeat), so the module's age rule works.
- **Docker access:** the Docker socket is root on its host. The checker should need **read-only** access only.
  How (a read-only socket proxy, or a documented `GET`-only setup): **open**.
- **Python 3.11 or newer** (decided 2026-11-11): 3.11 brings `tomllib` and is what Debian 12 / Raspberry Pi OS
  ship. Developed on 3.14; CI tests both ends.
- **Installable with uv or pip**; PyPI later. A container image for those who run everything in Docker.

## The module

**Decided: the module fetches the results from the checker over HTTP.** Weighed on 2026-11-11 against:
- **MQTT:** needs a broker, which makes adoption harder; a retained message could look like a live "OK".
- **The checker posting to MagicMirror:** MagicMirror itself has no general HTTP interface for sending a module
  data, but each module's `node_helper` gets MagicMirror's own web server (`this.expressApp`), so a module could
  add an address of its own. Easier for a user, but the checker must then know every display, and a display that
  restarts is empty until the next post.

Fetching instead: a dead checker shows at once (the fetch fails, or the data is too old), a restarted display has
the state on its first fetch, and no broker or extra module is needed. The checker's address goes in the module's
`config.js` entry.

**UI (first draft):**
- "All OK" when everything is green; otherwise only the non-green cards, red above amber, each with "since …".
- Collapse to a count per group when there are more than N items.
- Debounce, so a check that flips back and forth doesn't flicker.
- Smooth transitions.

## Quality bar

It has to be easy for other people:
- **Examples that run without any setup:** ping `localhost`; the TLS expiry of a public site; a "last updated"
  check against a SQLite file the example creates itself; a custom-function check; a "write a result by hand" example
  to show that any language works.
- **A Docker example** (needs only Docker): pending image updates for the running containers. A second one scans for
  vulnerabilities, marked as needing Trivy.
- **Docs:** a README with a screenshot or GIF, a 5-minute quick start, a config table, `SPEC.md`, a `CHANGELOG`,
  semver releases.
- **Tests and CI (GitHub Actions):** Python with pytest, ruff and type hints; the module with ESLint. Pass the
  automated checks that MagicMirror's 3rd-party module list runs, and get listed once v1 works.
- **Nothing of anyone's own setup in this repo:** no hostnames, IP addresses, database names or passwords, except in
  clearly fake examples.

## First version, in this order

1. `SPEC.md`: the result format, versioned, with the age rule.
2. The module, showing a hand-written results file served by a trivial web server.
3. The checker: the runner, the TOML config, the HTTP endpoint, the SQLite memory, and the first check types:
   `ping`, `sql_age`, a custom function.
4. The other check types, Docker ones included.

## Open

- **MQTT in v1 at all?** Proposal: no. A job that has to report itself (push) sends an HTTP POST to the checker's
  port, in the same format as a result.
- **Read-only Docker access** (above).
- **Limits in `config.js`** instead of the checker, for users who only want MMM-MysqlQuery-style checks? Not in v1.
- **Touch screens:** tap to expand a card? Not in v1.
- **The name:** free on 2026-11-11 (not in the module list on MagicMirror's wiki, no GitHub repository, not on npm);
  still to confirm by hand on modules.magicmirror.builders. **AnyStatus** is also an open-source Windows dashboard
  for CI/CD (github.com/AnyStatus/AnyStatus): not a conflict, but searches may find it first.
