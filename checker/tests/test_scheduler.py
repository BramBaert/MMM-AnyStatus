"""Scheduling rules, with a fake clock and a fake launcher (plus one test with a real hung thread)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import support_checks
from helpers import FakeClock, FakeLauncher, World, make_world

from anystatus.document import Snapshot
from anystatus.model import Observation
from anystatus.scheduler import Scheduler, ThreadLauncher


def ok() -> Observation:
    return Observation(level="ok")


def test_first_runs_are_spread_over_the_first_seconds(tmp_path: Path) -> None:
    text = "\n".join(f'[[check]]\nid = "c{i}"\ntype = "scripted"\ninterval = "1m"\n' for i in range(3))
    w = make_world(text, tmp_path, spread=6.0)
    start = w.clock.mono
    launched_at: dict[str, float] = {}
    for _ in range(80):
        w.sched.tick()
        for cid, mono in w.launcher.times:
            launched_at.setdefault(cid, mono - start)
        w.clock.advance(0.1)
    assert launched_at["c0"] == 0
    assert 1.9 < launched_at["c1"] < 2.2
    assert 3.9 < launched_at["c2"] < 4.2


def test_the_interval_is_measured_start_to_start(one_check: Callable[..., World]) -> None:
    w = one_check()
    w.sched.tick()
    t0 = w.clock.mono
    w.clock.advance(25)  # the run takes 25 s
    w.launcher.complete("c", ok(), w.clock)
    w.sched.tick()
    w.clock.advance(34.9)
    w.sched.tick()
    assert w.launcher.count("c") == 1  # t0 + 59.9: not yet
    w.clock.advance(0.2)
    w.sched.tick()
    assert w.launcher.count("c") == 2
    assert w.launcher.times[1][1] == t0 + 60 + 0.1 or abs(w.launcher.times[1][1] - (t0 + 60.1)) < 1e-6


def test_next_wakeup_points_at_the_next_start(one_check: Callable[..., World]) -> None:
    w = one_check()
    assert w.sched.next_wakeup(cap=100) == 0
    w.sched.tick()
    w.launcher.complete("c", ok(), w.clock)
    w.sched.tick()
    assert w.sched.next_wakeup(cap=100) == 60


def test_a_run_that_times_out_is_a_failure_and_the_hung_thread_is_not_restarted(
    one_check: Callable[..., World],
) -> None:
    w = one_check('timeout = "10s"\nconfirm = 1')
    w.sched.tick()  # started, and never returns
    w.clock.advance(9.9)
    w.sched.tick()
    assert w.result("c")["summary"] == "waiting for the first run"
    w.clock.advance(0.2)
    w.sched.tick()
    r = w.result("c")
    assert (r["level"], r["summary"]) == ("unknown", "timed out after 10 s")
    assert r["ts"] == "2026-11-11T12:00:10Z"
    for _ in range(100):  # an hour and more: the interval passes many times
        w.clock.advance(60)
        w.sched.tick()
    assert w.launcher.count("c") == 1
    assert w.result("c")["ts"] == "2026-11-11T12:00:10Z"  # it ages out, honestly
    # when the thread does end, its late result is discarded and the check starts again
    w.launcher.complete("c", ok(), w.clock)
    w.sched.tick()
    assert w.launcher.count("c") == 2
    assert w.result("c")["level"] == "unknown"


def test_runs_cannot_overlap_even_when_the_timeout_equals_the_interval(tmp_path: Path) -> None:
    text = '[[check]]\nid = "c"\ntype = "scripted"\ninterval = "30s"\ntimeout = "30s"\n'
    w = make_world(text, tmp_path)
    w.sched.tick()
    for _ in range(50):
        w.clock.advance(15)
        w.sched.tick()
        assert sum(1 for h in w.launcher.launched["c"] if not h.done()) <= 1
    assert w.launcher.count("c") == 1


def test_a_run_that_finishes_just_in_time_does_not_time_out(one_check: Callable[..., World]) -> None:
    w = one_check('timeout = "10s"')
    w.sched.tick()
    w.clock.advance(9)
    w.launcher.complete("c", ok(), w.clock)
    w.clock.advance(2)  # the scheduler only looks now, after the timeout: a finished run still counts
    w.sched.tick()
    assert w.result("c")["level"] == "ok"


def test_a_real_hung_thread_is_not_started_again(tmp_path: Path) -> None:
    support_checks.calls.clear()
    support_checks.gate.clear()
    text = """
[[check]]
id = "h"
type = "python"
function = "support_checks:blocker"
interval = "30s"
timeout = "5s"
"""
    clock = FakeClock()
    wake = threading.Event()
    from helpers import parse

    config = parse(text, tmp_path)
    snapshot = Snapshot("t")
    sched = Scheduler(
        config.checks, clock=clock, launcher=ThreadLauncher(clock, wake), snapshot=snapshot, spread=0
    )
    sched.start()
    try:
        sched.tick()
        wake.clear()
        for _ in range(20):
            clock.advance(30)
            sched.tick()
        assert support_checks.calls == ["start"]  # one thread, never a second
        assert sched.results()[0]["summary"] == "timed out after 5 s"
    finally:
        support_checks.gate.set()
    handle = sched.slots[0].zombie
    assert handle is not None
    for _ in range(200):
        if handle.done():
            break
        wake.wait(0.05)
    assert handle.done()
    sched.tick()
    clock.advance(30)
    sched.tick()
    assert support_checks.calls[:2] == ["start", "start"]  # started again once the thread had ended


def test_a_real_run_finishes_through_the_thread_launcher(tmp_path: Path) -> None:
    from helpers import parse

    config = parse(
        '[[check]]\nid = "n"\ntype = "python"\nfunction = "support_checks:number"\nn = 7\nwarn = 5\n',
        tmp_path,
    )
    clock = FakeClock()
    wake = threading.Event()
    sched = Scheduler(
        config.checks, clock=clock, launcher=ThreadLauncher(clock, wake), snapshot=Snapshot("t"), spread=0
    )
    sched.start()
    sched.tick()
    assert wake.wait(5)
    sched.tick()
    r = sched.results()[0]
    assert (r["level"], r["value"]) == ("warn", 7)


def test_launcher_is_injectable(tmp_path: Path) -> None:
    w = make_world('[[check]]\nid = "c"\ntype = "scripted"\n', tmp_path)
    assert isinstance(w.launcher, FakeLauncher)
