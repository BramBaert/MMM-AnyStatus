# MMM-AnyStatus

A calm status display for [MagicMirror²](https://magicmirror.builders/): a big **All OK** when everything is fine,
otherwise only what needs attention, with the reason and how long it has been like that.

- **Red** (`crit`): broken now. **Amber** (`warn`): can wait until the weekend. **Grey** (`unknown`): no recent
  result, so the display can't tell. A missing or stale status never passes for a good one.
- The module only *displays*. The levels are decided by whatever produces the results, in the format of
  [SPEC.md](SPEC.md). Any language that can write JSON can be a producer.
- No runtime dependencies, no build step.

> **Status: work in progress.** The module works against any server that follows [SPEC.md](SPEC.md). The checker
> (a Python service that runs the checks and serves the results) is coming; see [docs/design.md](docs/design.md).
> Until then, try it with the hand-written example below.

## Quick start

1. Clone the module into MagicMirror's `modules` folder (it has no dependencies, so there is nothing to install):

   ```sh
   cd ~/MagicMirror/modules
   git clone https://github.com/BramBaert/MMM-AnyStatus
   ```

2. Add it to `config/config.js`:

   ```js
   {
     module: "MMM-AnyStatus",
     position: "top_left",
     config: {
       checkerUrl: "http://checker.example:8099", // the module fetches <checkerUrl>/results
     },
   },
   ```

3. Restart MagicMirror.

### Try it without a checker

`examples/hand-written/` has a hand-written results file and a tiny web server (Python 3.9+, standard library only):

```sh
uv run --no-project --python 3.14 examples/hand-written/serve.py --demo
```

Point `checkerUrl` at `http://localhost:8099`. `--demo` shifts the times in the file on every request so that the
display shows all four levels (plus one deliberately stale result, which shows grey). A real producer never does
that: `ts` is when a check *ran*. Without `--demo` the file is served as it is, and goes grey once its `ttl` has
run out.

To see it in a real MagicMirror without installing anything but Docker, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Configuration

| Option | Default | Description |
|---|---|---|
| `checkerUrl` | none, **required** | Base URL of the checker (or any producer of [SPEC.md](SPEC.md)). The module fetches `<checkerUrl>/results`. |
| `fetchInterval` | `60000` | Milliseconds between fetches (at least 5000). |
| `maxItems` | `6` | Show at most this many cards. With more non-green items, the display collapses to one count per `group`. |

You can use the module more than once, each with its own `checkerUrl`.

## What the display does

- **All green:** "All OK".
- **Otherwise:** only the non-green items, red first, then grey, then amber. Each shows its label, the reason, and
  how long it has been so ("since 3 d").
- **The age rule**, applied every 15 seconds on the display itself, not only when data arrives: a result older than
  `ts + ttl` turns grey ("no data"), and one from more than `ttl` in the future turns grey ("clock mismatch"). If
  the checker can't be reached, the last results stay and age out by themselves, and one grey item says "unreachable".
- Anything it can't read (an invalid result, a duplicate `id`, an unknown level) is shown grey, never dropped. If the
  whole answer is unusable (not a valid document, or an unsupported spec version), the last valid results stay and
  age out, plus one grey item.
- A valid document without results is grey ("no checks reported"), not "All OK".
- If MagicMirror's node_helper has been silent for more than 3 fetch intervals, one grey item says so.

## Licence

MIT, see [LICENSE](LICENSE).
