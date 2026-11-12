# Contributing

Node is not needed on your machine: everything runs in Docker (Docker Desktop or Docker Engine with Compose).
The module has no runtime dependencies; ESLint and Ajv (used by the tests to check the schema) are dev dependencies.

```sh
docker compose -f compose.dev.yaml run --rm node npm install   # once; node_modules lives in a named volume
docker compose -f compose.dev.yaml run --rm node npm test
docker compose -f compose.dev.yaml run --rm node npm run lint
```

The logic is in `lib/status.js` (pure, takes `now` as an argument, runs in the browser and in Node) and
`lib/fetcher.js` (the node_helper's fetching). Tests are in `test/`, using Node's built-in runner. The tests also check
that the example in `SPEC.md`, `schema/results-1.schema.json` and the demo results agree with the code.

## See it in MagicMirror

```sh
docker compose -f compose.dev.yaml up magicmirror
```

Then open <http://localhost:8080>: three instances, one per column. Left rotates (every 20 s) through a healthy checker's states (all OK, mixed, stale and clock mismatch), the middle one shows many problems collapsed into groups, and the right one rotates through a broken checker's states (invalid results, empty, unsupported spec, unreachable). The demo server rotates through any list of scenarios: `/rotate/<name>,<name>,.../results`. The scenarios are the files in
examples/hand-written/scenarios/ (times are relative to now: x-demo-age and x-demo-since, in seconds, which
serve.py --demo turns into ts and since); the page is dev/config.js. This starts the demo results server (`examples/hand-written/serve.py --demo`) and
MagicMirror (server only, image pinned in `compose.dev.yaml`) with this folder mounted as
`modules/MMM-AnyStatus` and `dev/config.js` as its config. Edit the module, reload the browser; a change to
`node_helper.js` needs `docker compose -f compose.dev.yaml restart magicmirror`.

The fourth instance, under the left column, shows the real checker (`checker/`, running `dev/checker.toml`: the
example checks, three mqtt checks against the compose `mosquitto`, and three Docker checks through a socket proxy
that allows only the paths they use, `image_vulns` among them, with Trivy copied in from its own image by the `trivy-bin` service). It starts
with MagicMirror, or alone with `docker compose -f compose.dev.yaml up -d checker`. MagicMirror reaches it inside the
compose network; its own pages are also published to this PC (127.0.0.1 only, not on the network):
<http://localhost:8099/> (an overview), <http://localhost:8099/results> (the JSON) and
<http://localhost:8099/vulns> (the vulnerabilities of the dev stack's own images). The first `image_vulns` run
downloads Trivy's database into a volume, so `/vulns` says "No run yet." for a few minutes after the first start. The
demo server also listens on 8099, inside the network only.

Without Docker for MagicMirror, run the demo server alone:
`uv run --no-project --python 3.14 examples/hand-written/serve.py --demo`.

## Rules

- Keep the runtime dependency-free: no npm packages, no build step.
- The module displays; it does not decide levels (see `SPEC.md`).
- Nothing of anyone's own setup in this repo: no real hostnames, addresses or passwords, only clearly fake examples.
