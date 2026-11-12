"""`result_file`: another program's results, passed through as they are (not judged, debounced or re-stamped)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from helpers import START, FakeClock, World, make_world, parse

from anystatus.checks import result_file
from anystatus.judge import judge
from anystatus.model import Failure, Observation, Passthrough
from anystatus.scheduler import execute
from anystatus.store import Store
from anystatus.table import ConfigError

SCHEMA = json.loads((Path(__file__).parents[2] / "schema" / "results-1.schema.json").read_text(encoding="utf-8"))
TS = "2026-11-11T11:59:00Z"  # a minute before the fake clock's start (12:00:00)
CHECK = 'id = "jobs"\ntype = "result_file"\npath = "r.json"\ninterval = "1m"\n'  # ttl: 3 x 1 min = 180 s


@pytest.fixture(autouse=True)
def no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(result_file, "_RETRY_AFTER", 0)


def res(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "a", "level": "ok", "ts": TS, "ttl": 600}
    base.update(fields)
    return {k: v for k, v in base.items() if v is not None}


def doc(*results: Any, spec: str = "1.0") -> dict[str, Any]:
    return {"spec": spec, "source": "job", "results": list(results)}


def write(tmp_path: Path, content: Any, name: str = "r.json") -> None:
    text = content if isinstance(content, str) else json.dumps(content)
    (tmp_path / name).write_text(text, encoding="utf-8")


def world(tmp_path: Path, extra: str = "", **kwargs: Any) -> World:
    return make_world(f"[[check]]\n{CHECK}{extra}\n", tmp_path, **kwargs)


def run_once(w: World, check_id: str = "jobs") -> list[dict[str, Any]]:
    """One real run of a check (it reads the real file), then everything the checker now serves."""
    w.sched.tick()
    spec = next(s for s in w.config.checks if s.id == check_id)
    w.launcher.complete(check_id, execute(spec), w.clock)
    w.sched.tick()
    w.clock.advance(spec.interval)
    return w.sched.results()


def served(tmp_path: Path, content: Any, extra: str = "") -> list[dict[str, Any]]:
    write(tmp_path, content)
    results = run_once(world(tmp_path, extra))
    check_valid(results)
    return results


def check_valid(results: list[dict[str, Any]]) -> None:
    """What is served is a valid document, with unique ids (the rules a schema can't express included)."""
    jsonschema.validate({"spec": "1.0", "results": results}, SCHEMA)
    ids = [r["id"] for r in results]
    assert len(ids) == len(set(ids))
    for r in results:
        assert r["since"] <= r["ts"] if "since" in r else True


# a single result


def test_a_single_result_is_served_under_the_checks_id_and_label(tmp_path: Path) -> None:
    results = served(
        tmp_path,
        res(id="whatever", level="warn", summary="slow", value=3.5, unit="s", since="2026-11-11T10:00:00Z"),
        'label = "Nightly job"\ngroup = "jobs"',
    )
    assert results == [
        {
            "id": "jobs",
            "label": "Nightly job",
            "group": "jobs",
            "level": "warn",
            "summary": "slow",
            "value": 3.5,
            "unit": "s",
            "since": "2026-11-11T10:00:00Z",
            "ts": TS,
            "ttl": 600,
        }
    ]


def test_the_files_own_label_and_group_win(tmp_path: Path) -> None:
    results = served(tmp_path, res(label="From file", group="g2"), 'label = "Nightly"\ngroup = "g1"')
    assert (results[0]["label"], results[0]["group"]) == ("From file", "g2")


def test_without_a_label_the_label_is_the_checks_which_defaults_to_its_id(tmp_path: Path) -> None:
    results = served(tmp_path, res())
    assert results[0]["label"] == "jobs" and "group" not in results[0]


def test_a_file_with_a_byte_order_mark_is_read(tmp_path: Path) -> None:
    (tmp_path / "r.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(res()).encode())
    assert run_once(world(tmp_path))[0]["level"] == "ok"


def test_fields_the_spec_does_not_know_are_not_passed_on(tmp_path: Path) -> None:
    results = served(tmp_path, res(**{"x-job": 1, "secret": "s"}))
    assert set(results[0]) == {"id", "label", "level", "ts", "ttl"}


def test_a_since_later_than_ts_is_cut_to_ts(tmp_path: Path) -> None:
    results = served(tmp_path, res(since="2026-11-12T00:00:00Z"))
    assert results[0]["since"] == TS


# a document


def test_a_document_is_served_under_prefixed_ids(tmp_path: Path) -> None:
    results = served(
        tmp_path,
        doc(res(id="db", label="Database dump", group="backups"), res(id="files", level="crit", summary="x")),
        'label = "Jobs"\ngroup = "jobs"',
    )
    assert [r["id"] for r in results] == ["jobs/db", "jobs/files"]
    assert (results[0]["label"], results[0]["group"]) == ("Database dump", "backups")
    # without a label of its own: named by its own id (not the check's label, which all would share); the group is
    # the check's
    assert (results[1]["label"], results[1]["group"]) == ("files", "jobs")
    assert results[1]["level"] == "crit"


def test_a_document_with_a_higher_minor_version_is_fine(tmp_path: Path) -> None:
    assert served(tmp_path, doc(res(), spec="1.7"))[0]["id"] == "jobs/a"


def test_an_empty_document_is_unknown(tmp_path: Path) -> None:
    results = served(tmp_path, doc())
    assert [(r["id"], r["level"], r["summary"]) for r in results] == [("jobs", "unknown", "invalid in file: no results")]


# not debounced, not re-stamped


def test_the_level_is_not_debounced_and_ts_is_not_restamped(tmp_path: Path) -> None:
    w = world(tmp_path, "confirm = 3\n")
    write(tmp_path, res(level="ok", ts="2026-11-11T11:59:30Z"))
    first = run_once(w)[0]
    assert first["level"] == "ok"
    write(tmp_path, res(level="crit", summary="broke", ts="2026-11-11T11:59:40Z", since="2026-11-11T11:59:40Z"))
    second = run_once(w)[0]
    assert (second["level"], second["summary"]) == ("crit", "broke")  # at once, although confirm = 3
    assert (second["since"], second["ts"]) == ("2026-11-11T11:59:40Z", "2026-11-11T11:59:40Z")
    # the job stops writing: the same file is read again and nothing is made fresher
    for _ in range(3):
        again = run_once(w)[0]
    assert again["ts"] == "2026-11-11T11:59:40Z"
    assert w.clock.now() - timedelta(seconds=1) > START  # the checker's clock moved on, the result's did not


def test_limits_and_templates_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="'warn' is not used"):
        parse(f"[[check]]\n{CHECK}warn = 1\n", tmp_path)
    for key in ("crit = 1", 'worse = "below"', 'summary = "x"'):
        with pytest.raises(ConfigError, match="is not used"):
            parse(f"[[check]]\n{CHECK}{key}\n", tmp_path)


# invalid entries


BAD: list[tuple[str, dict[str, Any], str]] = [
    ("no ts", res(ts=None), "no ts"),
    ("ts without offset", res(ts="2026-11-11T11:59:00"), "bad ts"),
    ("ts not a time", res(ts="yesterday"), "bad ts"),
    ("ts a number", res(ts=1762862340), "bad ts"),
    ("no ttl", res(ttl=None), "no ttl"),
    ("ttl zero", res(ttl=0), "bad ttl"),
    ("ttl negative", res(ttl=-5), "bad ttl"),
    ("ttl text", res(ttl="600"), "bad ttl"),
    ("ttl true", res(ttl=True), "bad ttl"),
    ("no level", res(level=None), "no level"),
    ("bad level", res(level="fine"), "bad level"),
    ("level number", res(level=1), "bad level"),
    ("since without offset", res(since="2026-11-11T10:00:00"), "bad since"),
    ("summary number", res(summary=5), "bad summary"),
    ("unit number", res(unit=5), "bad unit"),
    ("value bool", res(value=True), "bad value"),
    ("value list", res(value=[1]), "bad value"),
    ("label empty", res(label=""), "bad label"),
    ("group number", res(group=3), "bad group"),
]


@pytest.mark.parametrize(("name", "entry", "reason"), BAD, ids=[b[0] for b in BAD])
def test_an_invalid_result_in_a_document_is_unknown_under_its_id(tmp_path: Path, name: str, entry: dict[str, Any], reason: str) -> None:
    results = served(tmp_path, doc(res(id="good"), entry))
    assert [r["id"] for r in results] == ["jobs/good", "jobs/a"]
    assert results[0]["level"] == "ok"  # the others are not harmed
    bad = results[1]
    assert bad["level"] == "unknown"
    assert bad["summary"].startswith(f"invalid in file: {reason}")
    assert bad["ts"] == "2026-11-11T12:00:00Z"  # when the file was read
    assert bad["since"] == bad["ts"]
    assert bad["ttl"] == 180  # the check's


@pytest.mark.parametrize(("name", "entry", "reason"), BAD, ids=[b[0] for b in BAD])
def test_an_invalid_single_result_is_unknown_under_the_checks_id(tmp_path: Path, name: str, entry: dict[str, Any], reason: str) -> None:
    results = served(tmp_path, entry)
    assert len(results) == 1 and results[0]["id"] == "jobs"
    assert results[0]["summary"].startswith(f"invalid in file: {reason}")
    assert (results[0]["ts"], results[0]["ttl"]) == ("2026-11-11T12:00:00Z", 180)


def test_nan_and_infinity_are_invalid_not_served(tmp_path: Path) -> None:
    entry = json.dumps(res(ttl=60))[:-1] + ', "value": NaN}'  # json.dumps never writes NaN as it is not valid JSON
    text = '{"spec": "1.0", "results": [' + entry + "]}"
    results = served(tmp_path, text)
    assert results[0]["summary"] == "invalid in file: bad value"
    json.dumps(results, allow_nan=False)


def test_an_entry_that_is_not_an_object(tmp_path: Path) -> None:
    results = served(tmp_path, doc(res(id="good"), "text", 5))
    assert [r["id"] for r in results] == ["jobs/good", "jobs"]
    assert results[1]["summary"] == "invalid in file: 2 results without a usable id"


@pytest.mark.parametrize("entry", [res(id=None), res(id=5), res(id="-starts-with-dash"), res(id="has space")])
def test_an_entry_without_a_usable_id_is_unknown_under_the_checks_id(tmp_path: Path, entry: dict[str, Any]) -> None:
    results = served(tmp_path, doc(res(id="good"), entry))
    assert [r["id"] for r in results] == ["jobs/good", "jobs"]
    assert results[1]["level"] == "unknown" and results[1]["summary"].startswith("invalid in file: ")


def test_duplicate_ids_are_all_unknown_but_served_once(tmp_path: Path) -> None:
    results = served(tmp_path, doc(res(id="d"), res(id="x"), res(id="d", level="crit")))
    assert [(r["id"], r["level"]) for r in results] == [("jobs/d", "unknown"), ("jobs/x", "ok")]
    assert results[0]["summary"] == "invalid in file: id used more than once"


def test_the_combined_id_is_checked_against_the_128_character_limit(tmp_path: Path) -> None:
    long_id = "j" * 120
    extra = ""
    text = f'[[check]]\nid = "{long_id}"\ntype = "result_file"\npath = "r.json"\ninterval = "1m"\n{extra}\n'
    fits = "a" * 7  # 120 + "/" + 7 = 128
    too_long = "b" * 8  # 129
    write(tmp_path, doc(res(id=fits), res(id=too_long)))
    w = make_world(text, tmp_path)
    results = run_once(w, long_id)
    check_valid(results)
    assert [r["id"] for r in results] == [f"{long_id}/{fits}", long_id]
    assert len(results[0]["id"]) == 128
    assert results[1]["summary"] == "invalid in file: id too long"


# a file that can't be used at all


def only(results: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(results) == 1 and results[0]["id"] == "jobs"
    return results[0]


def test_a_missing_file_is_unknown(tmp_path: Path) -> None:
    r = only(run_once(world(tmp_path)))
    assert (r["level"], r["summary"]) == ("unknown", "file not found")
    assert (r["ts"], r["ttl"]) == ("2026-11-11T12:00:00Z", 180)
    check_valid([r])


@pytest.mark.parametrize(
    ("content", "summary"),
    [
        ("this is not json", "not JSON"),
        ("", "not JSON"),
        ('{"spec": "1.0", "results": [', "not JSON"),
        (json.dumps(doc(res(), spec="2.0")), "unsupported spec 2.0"),
        (json.dumps(doc(res(), spec="10.1")), "unsupported spec 10.1"),
        (json.dumps({"spec": 1, "results": []}), "no valid spec version in the document"),
        (json.dumps({"spec": "one", "results": []}), "no valid spec version in the document"),
        (json.dumps({"results": []}), "no valid spec version in the document"),
        (json.dumps({"spec": "1.0"}), "the document has no results list"),
        (json.dumps({"spec": "1.0", "results": {"a": 1}}), "the document has no results list"),
        (json.dumps([res()]), "not a result or a document"),
        ("42", "not a result or a document"),
        ("x" * (1024 * 1024 + 1), "file is larger than 1 MB"),
    ],
)
def test_a_file_that_cannot_be_used_is_one_unknown_result(tmp_path: Path, content: str, summary: str) -> None:
    write(tmp_path, content)
    r = only(run_once(world(tmp_path)))
    assert (r["level"], r["summary"]) == ("unknown", summary)
    assert (r["ts"], r["ttl"]) == ("2026-11-11T12:00:00Z", 180)
    check_valid([r])


def test_a_file_just_under_the_size_limit_is_read(tmp_path: Path) -> None:
    padding = "p" * (1024 * 1024 - 300)
    results = served(tmp_path, res(summary=padding))
    assert results[0]["level"] == "ok"


def test_a_path_that_is_a_folder_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "r.json").mkdir()
    r = only(run_once(world(tmp_path)))
    assert (r["level"], r["summary"]) == ("unknown", "cannot read the file")


def test_failures_take_the_normal_judge_and_debounce_path(tmp_path: Path) -> None:
    spec = parse(f"[[check]]\n{CHECK}", tmp_path).checks[0]
    outcome = execute(spec)
    assert outcome == Failure("file not found", outcome.detail if isinstance(outcome, Failure) else None)
    assert judge(spec, outcome).level == "unknown"
    # a good read followed by a gone file: debounced like any failure (confirm = 2), so the first miss keeps the
    # file's last result as it was (its own ts: it still ages out honestly) ...
    w = world(tmp_path)
    write(tmp_path, res(level="ok"))
    first = run_once(w)[0]
    assert first["level"] == "ok"
    (tmp_path / "r.json").unlink()
    assert run_once(w)[0] == first
    # ... the second miss shows; a single result shares the check's id, so the failure takes its place ...
    results = run_once(w)
    assert [(r["id"], r["level"], r["summary"]) for r in results] == [("jobs", "unknown", "file not found")]
    # ... and when it is back, so are its own results
    write(tmp_path, res(level="warn", ts="2026-11-11T12:01:30Z"))
    assert run_once(w)[0]["level"] == "warn"


def test_a_confirmed_failure_shows_next_to_a_documents_last_results(tmp_path: Path) -> None:
    """A document's results have their own ids and ts: after a confirmed failure they stay (and age out by their own
    ttl) next to the failure, so a red result can't vanish behind one grey "file not found"."""
    w = world(tmp_path)
    write(tmp_path, doc(res(id="db", level="crit", summary="dump failed")))
    kept = run_once(w)
    assert [r["id"] for r in kept] == ["jobs/db"]
    (tmp_path / "r.json").unlink()
    assert run_once(w) == kept  # not confirmed yet
    results = run_once(w)
    assert [(r["id"], r["level"]) for r in results] == [("jobs", "unknown"), ("jobs/db", "crit")]
    assert results[1] == kept[0]  # unchanged, own ts


def test_the_partial_write_is_read_again_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = parse(f"[[check]]\n{CHECK}", tmp_path).checks[0]
    write(tmp_path, '{"id": "a", "lev')
    original = result_file.ResultFile._read
    calls = {"n": 0}

    def fixing(self: result_file.ResultFile) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            write(tmp_path, res())  # the writer finished in between
        return original(self)

    monkeypatch.setattr(result_file.ResultFile, "_read", fixing)
    assert isinstance(execute(spec), Passthrough)
    assert calls["n"] == 2


# the scheduler


MIX = """
[[check]]
id = "first"
type = "scripted"
[[check]]
id = "docs"
type = "result_file"
path = "docs.json"
[[check]]
id = "second"
type = "scripted"
[[check]]
id = "single"
type = "result_file"
path = "single.json"
[[check]]
id = "third"
type = "scripted"
"""


def complete_all(w: World) -> None:
    w.sched.tick()
    for spec in w.config.checks:
        outcome = execute(spec) if spec.type == "result_file" else Observation(level="ok", summary="fine")
        w.launcher.complete(spec.id, outcome, w.clock)
    w.sched.tick()


def test_results_are_flattened_in_config_order(tmp_path: Path) -> None:
    write(tmp_path, doc(res(id="x"), res(id="y")), "docs.json")
    write(tmp_path, res(), "single.json")
    w = make_world(MIX, tmp_path)
    # before any run: every check says it is waiting (a passthrough check is still one result)
    assert [r["id"] for r in w.sched.results()] == ["first", "docs", "second", "single", "third"]
    complete_all(w)
    results = w.sched.results()
    assert [r["id"] for r in results] == ["first", "docs/x", "docs/y", "second", "single", "third"]
    check_valid(results)
    assert json.loads(w.snapshot.json_bytes())["results"] == results  # what is served


def test_a_check_that_serves_several_results_counts_as_one_run(tmp_path: Path) -> None:
    write(tmp_path, doc(res(id="x"), res(id="y")), "docs.json")
    write(tmp_path, res(), "single.json")
    w = make_world(MIX, tmp_path)
    assert not w.sched.first_runs_done()
    complete_all(w)
    assert w.sched.first_runs_done()


def test_the_served_document_is_valid_with_every_kind_of_result(tmp_path: Path) -> None:
    write(tmp_path, doc(res(id="x"), res(id="bad", ttl=0), res(id="x2", level="crit")), "docs.json")
    write(tmp_path, "not json", "single.json")
    w = make_world(MIX, tmp_path)
    complete_all(w)
    jsonschema.validate(json.loads(w.snapshot.json_bytes()), SCHEMA)


# the memory


def test_a_restart_waits_for_the_first_run_and_then_reads_the_file_again(tmp_path: Path) -> None:
    write(tmp_path, doc(res(id="x"), res(id="y")), "docs.json")
    write(tmp_path, res(), "single.json")
    clock = FakeClock()
    store = Store(tmp_path / "m.sqlite")
    w = make_world(MIX, tmp_path, store=store, clock=clock)
    complete_all(w)
    assert len(w.sched.results()) == 6
    assert {r[0] for r in store.conn.execute("SELECT id FROM checks")} == {
        "first",
        "second",
        "third",
    }  # no rows for them
    store.close()

    clock2 = FakeClock(clock.now() + timedelta(seconds=30))
    store2 = Store(tmp_path / "m.sqlite")
    w2 = make_world(MIX, tmp_path, store=store2, clock=clock2)  # prunes: must not trip over the passthrough checks
    waiting = {r["id"]: r for r in w2.sched.results()}
    assert waiting["docs"]["summary"] == "waiting for the first run"
    assert waiting["first"]["summary"] == "fine"  # the others continue from the memory
    complete_all(w2)
    assert [r["id"] for r in w2.sched.results()] == ["first", "docs/x", "docs/y", "second", "single", "third"]
    store2.close()


def test_the_memory_keeps_nothing_of_a_passthrough_check_after_it_failed_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.sqlite")
    w = world(tmp_path, store=store)
    run_once(w)  # no file: a failure, which is remembered like any other
    assert set(store.load_all()) == {"jobs"}
    write(tmp_path, doc(res(id="x"), res(id="y")))
    run_once(w)
    assert w.sched.results()[0]["id"] == "jobs/x"
    store.close()
    w2 = world(tmp_path, store=Store(tmp_path / "m.sqlite"))  # the next start prunes and loads without a problem
    assert w2.sched.results()[0]["id"] == "jobs"


# config


def test_a_relative_path_is_relative_to_the_config_file(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    write(sub, res(), "r.json")
    spec = parse('[[check]]\nid = "jobs"\ntype = "result_file"\npath = "sub/r.json"\n', tmp_path).checks[0]
    assert isinstance(execute(spec), Passthrough)


def test_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing 'path'"):
        parse('[[check]]\nid = "jobs"\ntype = "result_file"\n', tmp_path)
    with pytest.raises(ConfigError, match="unknown key 'paht'"):
        parse('[[check]]\nid = "jobs"\ntype = "result_file"\npath = "x"\npaht = "y"\n', tmp_path)
