from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import jsonschema
import pytest
from helpers import make_world

from anystatus.model import Failure, Observation, Reading
from anystatus.server import ResultsServer

SCHEMA = json.loads((Path(__file__).parents[2] / "schema" / "results-1.schema.json").read_text(encoding="utf-8"))

CFG = """
[checker]
name = "home-checker"
[[check]]
id = "a.ok"
type = "scripted"
group = "g"
[[check]]
id = "b.warn"
type = "scripted"
label = "Warn one"
[[check]]
id = "c.crit"
type = "scripted"
[[check]]
id = "d.unknown"
type = "scripted"
[[check]]
id = "e.waiting"
type = "scripted"
"""


def get(port: int, path: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status, resp.headers["Content-Type"], resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers["Content-Type"], err.read()


@pytest.fixture
def served(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = make_world(CFG, tmp_path)
    server = ResultsServer("127.0.0.1", 0, w.snapshot)
    server.start()
    yield w, server.port
    server.stop()


def test_the_served_document_is_valid_against_the_schema(served) -> None:  # type: ignore[no-untyped-def]
    w, port = served
    w.sched.tick()
    w.launcher.complete("a.ok", Reading(value=3, unit="ms", text="3 ms"), w.clock)
    w.launcher.complete("b.warn", Observation(level="warn", summary="meh", value="text"), w.clock)
    w.launcher.complete("c.crit", Observation(level="crit", summary="bad"), w.clock)
    w.launcher.complete("d.unknown", Failure("connection refused"), w.clock)
    w.sched.tick()
    status, ctype, body = get(port, "/results")
    assert (status, ctype) == (200, "application/json")
    doc = json.loads(body)
    jsonschema.validate(doc, SCHEMA)
    assert doc["source"] == "home-checker"
    assert [r["id"] for r in doc["results"]] == [
        "a.ok",
        "b.warn",
        "c.crit",
        "d.unknown",
        "e.waiting",
    ]  # config order
    assert doc["results"][0]["group"] == "g"
    for r in doc["results"]:
        assert r["since"] <= r["ts"]


def test_the_startup_document_is_valid_and_says_waiting(served) -> None:  # type: ignore[no-untyped-def]
    _, port = served
    doc = json.loads(get(port, "/results")[2])
    jsonschema.validate(doc, SCHEMA)
    assert {r["summary"] for r in doc["results"]} == {"waiting for the first run"}


def test_answering_never_runs_checks_and_never_touches_ts(served) -> None:  # type: ignore[no-untyped-def]
    w, port = served
    first = get(port, "/results")[2]
    w.clock.advance(3600)
    second = get(port, "/results")[2]
    assert first == second
    assert w.launcher.launched == {}


def test_overview_and_404(served) -> None:  # type: ignore[no-untyped-def]
    _, port = served
    status, ctype, body = get(port, "/")
    assert status == 200 and ctype.startswith("text/plain")
    assert b"a.ok" in body
    assert get(port, "/nope")[0] == 404
    assert get(port, "/results/")[0] == 404


def test_overview_shows_a_multi_line_summary_on_one_line(served) -> None:  # type: ignore[no-untyped-def]
    w, port = served
    w.sched.tick()
    w.launcher.complete("a.ok", Observation(level="warn", summary="first\nsecond\nthird"), w.clock)
    w.sched.tick()
    body = get(port, "/")[2].decode("utf-8")
    assert "first; second; third" in body
    assert "first\nsecond" not in body
    assert json.loads(get(port, "/results")[2])["results"][0]["summary"] == "first\nsecond\nthird"
