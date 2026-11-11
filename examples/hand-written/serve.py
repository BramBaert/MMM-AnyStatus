"""A trivial web server for a hand-written results file (stdlib only).

    uv run --no-project --python 3.14 examples/hand-written/serve.py --demo

GET /results answers with the file, as SPEC.md asks of a producer.

A producer must not touch `ts` when it serves a result again, so a plain static file goes grey once its `ttl` has
run out: that is the age rule working. Without --demo this server does exactly that.

--demo is for demos only, and not what a real producer does:
- /results: on every request it shifts ALL times in the file (`ts` and `since`) by the same amount, so the newest
  `ts` equals "now". The ages between results stay as written, so the display shows every level, and "Garage sensor"
  (written 4 hours older than the rest, with a 15 minute ttl) is deliberately stale and shows grey.
- /scenario/<name>/results: one of the files in scenarios/, each showing one state of the display. Their results have
  no real times but the demo-only fields `x-demo-age` (seconds since the check ran; negative = in the future) and
  `x-demo-since` (seconds since the level last changed), which become `ts` and `since` on every request (SPEC.md
  reserves `x-` fields for a producer's own use; consumers ignore them). Scenarios: all-ok, mixed, many, stale,
  invalid, empty, unsupported, unreachable (HTTP 503), and rotate (a new one of those every 20 seconds).
- /rotate/<name>,<name>,.../results: rotates through the scenarios you list, switching every 20 seconds, e.g.
  /rotate/all-ok,mixed,stale/results.
"""

import argparse
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TIME_FIELDS = ("ts", "since")
SCENARIO_DIR = Path(__file__).with_name("scenarios")
ROTATE_SECONDS = 20
ROTATION =("all-ok", "mixed", "many", "stale", "invalid", "empty", "unsupported", "unreachable")


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shift_to_now(document: dict) -> dict:
    """DEMO ONLY: shift every `ts` and `since` so that the newest `ts` is now."""
    results = document.get("results", [])
    newest = max(parse_time(r["ts"]) for r in results)
    delta = datetime.now(timezone.utc) - newest
    for result in results:
        for field in TIME_FIELDS:
            if field in result:
                result[field] = format_time(parse_time(result[field]) + delta)
    return document


def materialize(document: dict, now: datetime) -> dict:
    """DEMO ONLY: turn `x-demo-age` and `x-demo-since` (seconds before now) into `ts` and `since`."""
    for result in document.get("results", []):
        for demo_field, field in (("x-demo-age", "ts"), ("x-demo-since", "since")):
            seconds = result.pop(demo_field, None)
            if seconds is not None:
                result[field] = format_time(now - timedelta(seconds=seconds))
    return document


def rotating(names: tuple[str, ...] | list[str]) -> str:
    """The current one of `names`; it changes every ROTATE_SECONDS (all rotations of the same length change together)."""
    return names[int(datetime.now(timezone.utc).timestamp() // ROTATE_SECONDS) % len(names)]


def scenario_answer(name: str) -> tuple[int, bytes]:
    """The status and body for a scenario name; "rotate" picks the current one of ROTATION."""
    if name == "rotate":
        name = rotating(ROTATION)
    if name == "unreachable":
        return 503, b"service unavailable (demo scenario)\n"
    path = SCENARIO_DIR / f"{name}.json"
    if "/" in name or not path.is_file():
        return 404, b"unknown scenario\n"
    document = materialize(json.loads(path.read_text(encoding="utf-8")), datetime.now(timezone.utc))
    return 200, json.dumps(document).encode()


def make_handler(path: Path, demo: bool) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?")[0] == "/results":
                document = json.loads(path.read_text(encoding="utf-8"))
                if demo:
                    document = shift_to_now(document)
                self.reply(200, "application/json", json.dumps(document).encode())
            elif demo and self.path.startswith("/scenario/") and self.path.split("?")[0].endswith("/results"):
                name = self.path.split("?")[0][len("/scenario/") : -len("/results")]
                status, body = scenario_answer(name)
                self.reply(status, "application/json" if status == 200 else "text/plain; charset=utf-8", body)
            elif demo and self.path.startswith("/rotate/") and self.path.split("?")[0].endswith("/results"):
                names = [n for n in self.path.split("?")[0][len("/rotate/") : -len("/results")].split(",") if n]
                status, body = scenario_answer(rotating(names)) if names else (404, b"no scenarios\n")
                self.reply(status, "application/json" if status == 200 else "text/plain; charset=utf-8", body)
            elif self.path == "/":
                self.reply(200, "text/plain; charset=utf-8", b"Hand-written results server. See /results.\n")
            else:
                self.reply(404, "text/plain; charset=utf-8", b"not found\n")

        def reply(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=Path(__file__).with_name("results.json"))
    parser.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 inside a container")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--demo", action="store_true", help="DEMO ONLY: shift all times so the newest ts is now")
    args = parser.parse_args()
    if args.demo:
        print("DEMO MODE: times in the file are shifted on every request. A real producer never does this.")
    print(f"Serving {args.file} at http://{args.host}:{args.port}/results")
    ThreadingHTTPServer((args.host, args.port), make_handler(args.file, args.demo)).serve_forever()


if __name__ == "__main__":
    main()
