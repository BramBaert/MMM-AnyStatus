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

Then open <http://localhost:8080>: one page with an instance of the module per display state, each with a header naming its
scenario (all OK, mixed, many/collapsed groups, stale and clock mismatch, invalid results, empty, unsupported spec,
unreachable, and one that rotates through all of them every 20 s). The scenarios are the files in
examples/hand-written/scenarios/ (times are relative to now: x-demo-age and x-demo-since, in seconds, which
serve.py --demo turns into 	s and since); the page is dev/config.js. This starts the demo results server (`examples/hand-written/serve.py --demo`) and
MagicMirror (server only, image pinned in `compose.dev.yaml`) with this folder mounted as
`modules/MMM-AnyStatus` and `dev/config.js` as its config. Edit the module, reload the browser; a change to
`node_helper.js` needs `docker compose -f compose.dev.yaml restart magicmirror`.

Without Docker for MagicMirror, run the demo server alone:
`uv run --no-project --python 3.14 examples/hand-written/serve.py --demo`.

## Rules

- Keep the runtime dependency-free: no npm packages, no build step.
- The module displays; it does not decide levels (see `SPEC.md`).
- Nothing of anyone's own setup in this repo: no real hostnames, addresses or passwords, only clearly fake examples.
