"""The SQLite memory: continuity within the ttl, a fresh start past it, "waiting for the first run", pruning."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from helpers import START, FakeClock, make_world

from anystatus.model import Observation
from anystatus.store import Store

TWO = """
[[check]]
id = "a"
type = "scripted"
interval = "1m"
confirm = 2
[[check]]
id = "b"
type = "scripted"
interval = "1m"
"""


def ok() -> Observation:
    return Observation(level="ok", summary="fine")


def crit() -> Observation:
    return Observation(level="crit", summary="broken")


def build(tmp_path: Path, clock: FakeClock, text: str = TWO):  # type: ignore[no-untyped-def]
    return make_world(text, tmp_path, store=Store(tmp_path / "m.sqlite"), clock=clock)


def test_the_file_uses_wal(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.sqlite")
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_no_stored_row_is_waiting_for_the_first_run(tmp_path: Path) -> None:
    clock = FakeClock()
    w = build(tmp_path, clock)
    r = w.result("a")
    assert (r["level"], r["summary"], r["ts"], r["ttl"]) == (
        "unknown",
        "waiting for the first run",
        "2026-11-11T12:00:00Z",
        180,
    )


def test_restart_within_ttl_keeps_since_and_the_pending_streak(tmp_path: Path) -> None:
    clock = FakeClock()
    w = build(tmp_path, clock)
    w.run(ok(), "a")  # 12:00 ok
    w.run(crit(), "a")  # 12:01 first bad run: pending (count 1)
    # the checker is restarted 90 s after the last run: within the ttl of 180 s
    clock2 = FakeClock(clock.now() + timedelta(seconds=30))
    w2 = build(tmp_path, clock2)
    r = w2.result("a")
    assert (r["level"], r["since"], r["ts"]) == ("ok", "2026-11-11T12:00:00Z", "2026-11-11T12:01:00Z")
    r = w2.run(crit(), "a")  # the streak continues: this is the second bad run, so it confirms
    assert r["level"] == "crit"
    assert r["since"] == "2026-11-11T12:01:00Z"  # the streak began before the restart


def test_restart_past_ttl_serves_the_old_result_but_resets_since_and_streak(tmp_path: Path) -> None:
    clock = FakeClock()
    w = build(tmp_path, clock)
    w.run(ok(), "a")
    w.run(crit(), "a")  # pending
    clock2 = FakeClock(clock.now() + timedelta(seconds=600))
    w2 = build(tmp_path, clock2)
    before = w2.result("a")
    assert (before["level"], before["ts"]) == ("ok", "2026-11-11T12:01:00Z")  # old, honest: its ts is old
    assert clock2.now() > START + timedelta(seconds=1 + 180)
    r = w2.run(crit(), "a")
    assert r["level"] == "crit"  # the old streak did not count: fresh start, first run is adopted
    assert r["since"] == "2026-11-11T12:12:00Z"  # since is this run, not the old streak's


def test_removed_ids_are_pruned_and_a_renamed_id_is_new(tmp_path: Path) -> None:
    clock = FakeClock()
    w = build(tmp_path, clock)
    w.run(ok(), "a")
    w.run(ok(), "b")
    store = Store(tmp_path / "m.sqlite")
    assert set(store.load_all()) == {"a", "b"}
    store.close()
    only_b = TWO.replace('id = "a"', 'id = "renamed"')
    w2 = build(tmp_path, clock, only_b)
    assert set(Store(tmp_path / "m.sqlite").load_all()) == {"b"}
    assert w2.result("renamed")["summary"] == "waiting for the first run"


def test_deleting_the_file_costs_only_since(tmp_path: Path) -> None:
    clock = FakeClock()
    w = build(tmp_path, clock)
    w.run(crit(), "a")
    w.sched.store.close()  # type: ignore[union-attr]
    (tmp_path / "m.sqlite").unlink(missing_ok=True)
    for extra in ("-wal", "-shm"):
        (tmp_path / f"m.sqlite{extra}").unlink(missing_ok=True)
    w2 = build(tmp_path, clock)
    assert w2.result("a")["summary"] == "waiting for the first run"
