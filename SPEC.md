# MMM-AnyStatus result format, spec 1.0

The format in which a *producer* (the MMM-AnyStatus checker, or anything else) hands status results to a
*consumer* (the MagicMirror module, or anything else). Any language that can write JSON can produce it.

**Status: draft.** Until MMM-AnyStatus v1 is released this may still change without a version bump.

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## The principle

A status display is only useful if a missing or stale status can never pass for a good one. Two rules follow, and
the rest of this spec serves them:

1. **Every result says when it was made and how long it may be believed** (`ts` and `ttl`). A consumer that
   finds it too old shows it as `unknown`, whatever its level says ([the age rule](#the-age-rule)).
2. **Nothing disappears silently.** A result the consumer can't read, a producer it can't reach, a version it
   doesn't understand: each is shown as `unknown`, never dropped.

## The document

Results travel in one JSON document (UTF-8, `Content-Type: application/json`):

```json
{
  "spec": "1.0",
  "source": "home-checker",
  "results": [ ... ]
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `spec` | string | yes | Version of this spec, `"<major>.<minor>"`. See [versioning](#versioning). |
| `source` | string | no | A name for the producer, for messages such as "home-checker unreachable". |
| `results` | array | yes | The results, one per check, in any order. May be empty. |

## A result

```json
{
  "id": "backup.nas",
  "label": "NAS backup",
  "group": "backups",
  "level": "crit",
  "summary": "last backup 3 days ago",
  "value": 3.1,
  "unit": "days",
  "since": "2026-11-09T02:15:00Z",
  "ts": "2026-11-11T14:03:00Z",
  "ttl": 900
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | Unique within the document and stable over time. Matches `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`. |
| `level` | string | yes | `ok`, `warn`, `crit` or `unknown`. See [levels](#levels). |
| `ts` | string (time) | yes | When the check that made this result **ran**. Not when the document was served. |
| `ttl` | number | yes | Seconds after `ts` that the result may be believed. Greater than 0. |
| `label` | string | no | What the display shows. Defaults to `id`. SHOULD be at most about 40 characters. |
| `group` | string | no | For collapsing many results into one count per group. |
| `summary` | string | no | The reason, short: "last backup 3 days ago". SHOULD be given for any level other than `ok`, and SHOULD be at most about 80 characters. |
| `value` | number or string | no | The measured value, for display. |
| `unit` | string | no | Unit of `value`: `"days"`, `"%"`, `"ms"`. |
| `since` | string (time) | no | When the check got its current level. MUST NOT be later than `ts`. |

**Times** are RFC 3339 with an explicit offset: `2026-11-11T14:03:00Z` or `2026-11-11T15:03:00+01:00`.
Fractional seconds are allowed. A time without an offset is invalid, because its meaning depends on whose clock
reads it.

Consumers MUST ignore fields they don't know, so later minor versions can add fields. Field names starting with
`x-` are reserved for producers' own use and will never be given a meaning by this spec.

## Levels

| Level | Colour | Meaning |
|---|---|---|
| `ok` | green | Fine. |
| `warn` | amber | Needs attention, but it can wait until the weekend: pending updates, a certificate expiring in 3 weeks. Never alarming. |
| `crit` | red | Broken now: data is being lost, a backup is missing, a fixable critical vulnerability is left unfixed. If something that can wait shows up as `crit`, that check's limits are wrong. |
| `unknown` | grey | The producer can't tell whether things are fine: the check timed out, the source didn't answer, the check itself failed. |

**The producer decides the level; the consumer only displays it.** In particular any debouncing (not flipping
level on a single odd reading) is the producer's job: `level` is final, and `since` is when *that* level started.
The age rule is the consumer's only rule of its own.

A consumer that meets a level it doesn't know MUST show it as `unknown`.

## The age rule

A consumer MUST compare each result's `ts` with its own clock (`now`) and show the result as `unknown` when:

- **it is too old:** `now > ts + ttl`. The display shows "no data since `ts`"; or
- **it is from the future:** `ts > now + ttl`. The producer's clock and the consumer's clock disagree by more
  than the result's own lifetime, so its age can't be judged. The display shows "clock mismatch".

The age rule comes before everything else a result says, an unknown `level` included. When it applies, the
result's own `level` and `summary` are not shown as current. A consumer MAY still show them as history ("was:
crit, last backup 3 days ago").

This is what makes a dead producer, a dead network or a stuck check visible: none of them can hide behind a last
`ok`. It only works if producers keep to the next section.

## Producing results

- **`ts` is when the check ran.** A producer that serves its results again and again MUST NOT update `ts` when it
  serves them; only when the check runs again. Otherwise a check that has stopped running would look fresh
  forever.
- **Heartbeat.** A producer that runs checks on an interval SHOULD make a new result every run, with a new `ts`,
  even when nothing changed.
- **Choosing `ttl`.** `ttl` must cover the time between two runs plus everything that may delay a run: the check's
  own timeout, the consumer's fetch interval, and a margin for one late run. **About 3 × the check's interval**
  is a good default. For a daily check that is still enough: 72 hours. If one missed run should already show,
  use 1.5 × the interval.
- **`since` survives restarts.** A producer that restarts SHOULD remember when each level started, rather than
  reporting `since` as the time of the restart.
- **Failures of the check itself** (timeouts, a database that refuses the connection, an exception) are reported
  as `unknown` with the reason in `summary`, not as `crit`. Whether "can't reach the database" is critical is a
  separate check's decision.
- Producers SHOULD keep clocks in sync (NTP). So should consumers.

## Serving

A producer serving over HTTP:

- MUST answer `GET /results` with the document, status `200`, `Content-Type: application/json`.
- SHOULD answer quickly from results it already has. It MUST NOT run checks while answering.
- MAY serve other paths. `GET /` MAY be a human-readable page.

**Planned, not yet part of the spec:** jobs that have no other way to report (push) send `POST /results` to the
checker, with a body that is a document in this format.

## Consuming

A consumer:

- MUST apply [the age rule](#the-age-rule) to every result, every time it redraws, not only when it fetches.
  A display that is never refreshed must still turn grey.
- MUST show a producer it can't reach, whose answer isn't a valid document, or whose document has a `spec` major
  version it doesn't support, as one `unknown` item ("home-checker unreachable", "unsupported spec 2.0"). In all
  three cases it SHOULD keep the last valid results it had: they turn grey by themselves when their `ttl` runs out.
  A higher minor version of a supported major version is fine.
- MUST NOT drop a result because it is invalid: wrong or missing fields, a time that isn't RFC 3339 with an offset
  (`since` included), or an `id` used more than once. If the result has a readable `id`, it shows that id as
  `unknown` ("invalid result: no ttl"); otherwise one `unknown` item for the document. When an `id` is used more
  than once, every result with that `id` is shown as `unknown`.
- MUST show a valid document with no results as one `unknown` item ("no checks reported"), not as all fine. A
  producer that checks nothing is misconfigured.
- If `since` is later than `ts`, uses `ts`.
- MUST NOT let the age rule depend on the transport. If the consumer has parts (for MagicMirror: the browser and
  its `node_helper`), the part that draws applies the age rule and also notices when the part that fetches has gone
  quiet.

How a consumer words and formats these ("since 14:03" or "since 3 h") is up to it; the texts above are examples.

## Versioning

`spec` is `"<major>.<minor>"`:

- A **minor** version only adds optional fields, or meanings for values that consumers already treat safely (an
  unknown level is `unknown`). A consumer of 1.0 can read any 1.x.
- A **major** version is anything else. Consumers MUST refuse a major version they don't support (see above).

## Machine-readable schema

[`schema/results-1.schema.json`](schema/results-1.schema.json) is a JSON Schema (draft 2020-12) of the document.
It checks the shape. The rules it can't express are in this text and still apply: an offset on every time,
`since` not after `ts`, unique `id`s, and the age rule.

## A full example

```json
{
  "spec": "1.0",
  "source": "home-checker",
  "results": [
    {
      "id": "ping.router",
      "label": "Router",
      "group": "network",
      "level": "ok",
      "value": 1.8,
      "unit": "ms",
      "since": "2026-11-02T08:00:12Z",
      "ts": "2026-11-11T14:03:00Z",
      "ttl": 180
    },
    {
      "id": "docker.updates",
      "label": "Image updates",
      "group": "docker",
      "level": "warn",
      "summary": "3 images have a newer version",
      "value": 3,
      "since": "2026-11-10T06:00:00Z",
      "ts": "2026-11-11T06:00:00Z",
      "ttl": 259200
    },
    {
      "id": "backup.nas",
      "label": "NAS backup",
      "group": "backups",
      "level": "crit",
      "summary": "last backup 3 days ago",
      "value": 3.1,
      "unit": "days",
      "since": "2026-11-09T02:15:00Z",
      "ts": "2026-11-11T14:00:00Z",
      "ttl": 900
    }
  ]
}
```
