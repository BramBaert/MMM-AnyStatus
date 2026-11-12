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
    digest). Registry manifest HEAD requests, which Docker Hub doesn't count against its pull limit. **An image built
    locally is checked through its base image** (decided with Bram 2026-11-12, see below)
  - `image_vulns`: wraps Trivy's JSON output and sorts every CVE into a **class** by what the user can do about it,
    shown on a **schedule** per class and severity (decided with Bram 2026-11-12, see below). Optional, needs Trivy.
    Meant to run **daily**
  - `file_age`, `disk`
  - `exec` with Nagios exit codes, so existing check plugins work
  - a custom Python function, as the escape hatch for anything else
- **It remembers in a SQLite file of its own**, so a restart doesn't forget that a check is halfway its failing
  age. Most ages come from the source's own timestamps (read again after a restart); the file holds what has none:
  when a level started (`since`), the last pushed facts, the `show_after` streaks, when a CVE was first found. In a
  container: a volume, so it survives restarts and new versions. Losing it only resets those.
- **It serves its current results over HTTP**, on a port from its config. Each result again every round (a
  heartbeat), so the module's age rule works.
- **Docker access:** the Docker socket is root on its host. The checker should need **read-only** access only.
  How: **decided: docker-socket-proxy, see [checker.md](checker.md)** (`CONTAINERS=1`, `IMAGES=1`; mounting the socket
  `:ro` is not read-only).
- **Python 3.11 or newer** (decided 2026-11-11): 3.11 brings `tomllib` and is what Debian 12 / Raspberry Pi OS
  ship. Developed on 3.14; CI tests both ends.
- **Installable with uv or pip**; PyPI later. A container image for those who run everything in Docker.

### From the first real deployment (2026-11-12, checker at `c7f411a`)

Found while running the Docker checks against a real Docker host, decided with Bram on 2026-11-12, and built.
How each works in detail: [checker.md](checker.md).

- **Green is green.** `confirm` applied to every change of level, the way back too: with `confirm = 14` and
  `interval = "1d"` a card stayed amber up to 14 days after the problem was fixed. A change to a *better* level is
  now reported at once; only getting worse is delayed. Flapping around a limit is still absorbed, because the worse
  level has to last again before it shows.
- **`show_after` replaces `confirm`.** `confirm` × `interval` was a delay, but one number for all levels (so "amber
  at once, red after 7 days" couldn't be said), counted in runs, and named after nothing a user thinks of. Instead,
  in every check type, a time per level: `show_after = { warn = "0d", crit = "7d", unknown = "2m" }`.
  - **A level shows once it has lasted its time**; until then the highest lower level whose time has passed is shown
    (a fixable CRITICAL with the line above: amber at once, red after 7 days; with `warn = "14d"` it would be green
    for 7 days and then red, since amber's own time hasn't passed). A run that sees `crit` counts towards `warn` too,
    which is why getting better is always at once.
  - **At the first run at or after** the time has passed: never early, at most one `interval` late. A `show_after`
    shorter than the `interval` means "at the next run"; the config warns about it, as it warns about a `timeout`
    longer than the `interval`.
  - **Units:** only the `s`, `m`, `h`, `d` the durations already have; no months or years (a month is `"30d"`).
    `"0d"` means at once; `"never"` skips the level, written out rather than implied by a missing key.
  - **No default:** a level without a `show_after` shows at once. The examples set it where flapping matters (a
    dropped ping should not turn a card grey: `unknown = "2m"`), and `[defaults]` can hold it.
  - Why a time and not a count of runs (`show_after_n_intervals`, also considered): it means the same in every check
    type and in `image_vulns`' schedule, and it keeps its meaning when the `interval` changes.
  - `confirm` is gone (no release yet, so no compatibility shim): a config that still uses it is an error that
    names `show_after`.
- **`docker_updates`: locally built images.** With Docker's containerd image store (the default of new installs) an
  image built locally also has a `RepoDigest` (`myimage@sha256:…`), so it was asked about by its own name and
  reported "unknown (registry did not answer)". Docker 29's image inspect has an `Identity` field that says whether
  an image was built or pulled (found 2026-11-12); older engines fall back to "has RepoDigests". A locally built
  image's **base** is checked instead: Docker doesn't record the base's name (no label, the history doesn't name
  it), but an image's layers start with its base's, so the base is the local registry image whose layers are the
  longest prefix of ours. "myapp (base python:3.14-slim rebuilt, rebuild)". No list kept by hand, no labels. A base
  that was pruned can't be found; the summary says so instead of "unknown".
- **`image_vulns`: what counts, and how a user finds out what to do.** Every CVE with a fix *somewhere* used to
  count. In a public image that is often a fix only its maintainers can apply (a Go library inside a database image,
  fixed in a newer Go), so the card stayed red for weeks with nothing to do. Counting only fixes the user can apply
  is manageable, but then a critical hole without a fix stays invisible on one's own infrastructure, which is worse.
  - **Each CVE gets a class, by what the user can do about it:** *update* (a newer build of the same tag has the
    fix: pull it), *rebuild* (a locally built image whose base has a newer build with the fix, or whose own layers
    brought the package), *waiting* (the project released a fix, but no build of this tag has it yet), *no fix*
    (none exists). "Upstream" was rejected as the name of a class: it means nothing to a novice.
  - **Per class and severity, a schedule, counted per CVE from when it was first found** (Bram's model; its `warn`
    and `crit` times follow the rules of `show_after`, per CVE instead of per card): ignore it; or amber after *x*
    and red after *y*; or straight to red after *z*, skipping amber. The level of the card is the worst over all
    CVEs. A new critical doesn't inherit the age of an old one: "first found" is kept per CVE and image.
  - **Skipping a level is written out, not left out**, for the human reading the config: `warn = "never"`.
    Leaving a severity out of a class means "don't care", and the example config says so. The README has an
    example that explains the use case of each line.
  - **What it costs:** Trivy only says whether a fixed version exists, so *update* versus *waiting* means scanning
    the newer build of the tag too (straight from the registry), and *rebuild* the newer base. That is extra scans
    only for images that have a newer build or base; still one scan per image otherwise, and no CVE reported twice.
    *No fix* means dropping `--ignore-unfixed`.
  - **Trivy stays** (Bram: every design is a compromise; don't switch scanners because of a brainstorm). Whether a
    CVE is known to be exploited (CISA's KEV list): Trivy's JSON doesn't say (checked on Trivy 0.76.2; requests for
    it are open at Trivy), so nothing is built on it.
  - **The card's line is short**, because the module's CSS keeps cards narrow (Bram isn't fond of it, but wants to
    see it in practice first): image, count, severity, what to do. "myapp: 2 critical, fixed, please rebuild
    image", "mariadb: 1 critical, fixed, please update image", "mariadb: 1 critical, waiting for upstream fix"
    (Bram: for those in the know it says much more than "waiting for fix"), "mariadb: 1 critical, no fix exists".
    The full list (CVE, package, installed and fixed version, class, first found) is on a page of the checker's own,
    `/vulns` next to `/results`, since a display can't be clicked.

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
- Collapse to one row per group when there are more than N items: the group's count and the names of its checks,
  each in its level's colour, worst first, at most ~4 names then "+N more".
- No flicker when a check flips back and forth: the checker's `show_after` takes care of it, so the module shows what
  it gets.
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

- **MQTT as the transport in v1?** Proposal: no. A job that has to report itself (push) sends an HTTP POST to the
  checker's port, in the same format as a result. **MQTT as a source** is a different matter: a device's heartbeat
  or state on a topic is a status where it lives, so it gets a check type (`mqtt` in [checker.md](checker.md)).
- **Read-only Docker access:** decided (above).
- **Limits in `config.js`** instead of the checker, for users who only want MMM-MysqlQuery-style checks? Not in v1.
- **Touch screens:** tap to expand a card? Not in v1.
- **The name:** free on 2026-11-11 (not in the module list on MagicMirror's wiki, no GitHub repository, not on npm);
  still to confirm by hand on modules.magicmirror.builders. **AnyStatus** is also an open-source Windows dashboard
  for CI/CD (github.com/AnyStatus/AnyStatus): not a conflict, but searches may find it first.

### Still open from the first real deployment (2026-11-12)

What was decided then is built and described above ("From the first real deployment"). Still open:

- **Escalation by age:** "amber for longer than 60 days becomes red" (pending updates are never more than amber).
  `show_after` doesn't do that; it would be one more entry, e.g. `escalate = { crit = "60d" }`.
- **`image_vulns`: does a CVE's clock restart when its class changes?** As decided, a CVE is counted from when it
  was first found in an image, whatever its class. So a CVE that was *waiting* for 10 days becomes *update* when the
  newer build appears, and with `update`'s `CRITICAL = { crit = "3d" }` it is red at once, although the fix could
  only be pulled since today. Counting from when it entered its class would give the 3 days. Built as decided; to
  confirm.
- **`image_vulns`: a CVE in a locally built image's own layers** (a package its Dockerfile installs) is counted as
  *rebuild*: a fixed version exists and it is in the user's hands. The four classes didn't name this case. Rebuilding
  helps when the Dockerfile installs the newest version; if it pins one, the fix is a change to the Dockerfile.

## Future work

Not planned yet; how they play out in practice decides their priority.

- **Seen and accepted, with an end date** (Bram, 2026-11-12): a list in the config, e.g. `accept = [{ cve = "CVE-…",
  image = "mariadb", until = "2027-01-31", why = "…" }]`. An accepted CVE doesn't count until its date, then it is
  back. It keeps the display calm without a blind spot, and the reason is written down. Trivy's `.trivyignore` has
  the same idea (`exp:` dates).
