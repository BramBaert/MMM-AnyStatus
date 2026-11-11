"""The confirm streak, `since`, and `ts`: all with a fake clock, no sleeping."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from helpers import START, World

from anystatus.model import Failure, Observation


def ok(summary: str = "fine", value: float | None = None) -> Observation:
    return Observation(level="ok", summary=summary, value=value)


def crit(summary: str = "broken") -> Observation:
    return Observation(level="crit", summary=summary)


def test_first_run_is_reported_at_once(one_check: Callable[..., World]) -> None:
    w = one_check("confirm = 3")
    assert w.result("c")["summary"] == "waiting for the first run"
    r = w.run(crit())
    assert r["level"] == "crit"
    assert r["since"] == "2026-11-11T12:00:00Z"


def test_a_change_needs_confirm_runs_in_a_row(one_check: Callable[..., World]) -> None:
    w = one_check("confirm = 2")
    w.run(ok())
    r = w.run(crit("first bad run"))
    assert r["level"] == "ok"  # pending: the last confirmed result...
    assert r["summary"] == "fine"
    assert r["ts"] == "2026-11-11T12:01:00Z"  # ...with the new ts
    r = w.run(crit("second bad run"))
    assert r["level"] == "crit"
    assert r["summary"] == "second bad run"


def test_since_is_the_start_of_the_streak_not_the_confirmation(one_check: Callable[..., World]) -> None:
    w = one_check("confirm = 3")
    r = w.run(ok())
    assert r["since"] == "2026-11-11T12:00:00Z"
    w.run(crit())  # 12:01 starts the streak
    w.run(crit())  # 12:02
    r = w.run(crit())  # 12:03 confirms
    assert r["level"] == "crit"
    assert r["since"] == "2026-11-11T12:01:00Z"
    assert r["ts"] == "2026-11-11T12:03:00Z"


def test_flapping_never_confirms(one_check: Callable[..., World]) -> None:
    w = one_check("confirm = 2")
    w.run(ok())
    for _ in range(6):
        assert w.run(crit())["level"] == "ok"
        r = w.run(ok())
        assert r["level"] == "ok"
        assert r["since"] == "2026-11-11T12:00:00Z"


def test_confirm_one_reports_at_once(one_check: Callable[..., World]) -> None:
    w = one_check("confirm = 1")
    w.run(ok())
    r = w.run(crit())
    assert r["level"] == "crit"
    assert r["since"] == "2026-11-11T12:01:00Z"


def test_failures_are_confirmed_like_any_level(one_check: Callable[..., World]) -> None:
    w = one_check("confirm = 2")
    w.run(ok())
    assert (
        w.run(Failure("connection refused"))["level"] == "ok"
    )  # one dropped check does not turn the card grey
    r = w.run(Failure("connection refused"))
    assert (r["level"], r["summary"]) == ("unknown", "connection refused")


def test_a_change_of_value_or_summary_within_a_level_keeps_since(one_check: Callable[..., World]) -> None:
    w = one_check("confirm = 2")
    w.run(ok("5 ms", 5))
    r = w.run(ok("7 ms", 7))
    assert (r["summary"], r["value"], r["since"]) == ("7 ms", 7, "2026-11-11T12:00:00Z")
    assert r["ts"] == "2026-11-11T12:01:00Z"


def test_a_pending_change_that_goes_back_does_not_leave_a_streak(one_check: Callable[..., World]) -> None:
    w = one_check("confirm = 2")
    w.run(ok())
    w.run(crit())
    w.run(ok())  # back: streak gone
    assert w.run(crit())["level"] == "ok"  # starts again from one, so still not confirmed


def test_ts_is_when_the_run_finished(one_check: Callable[..., World]) -> None:
    w = one_check()
    w.sched.tick()
    w.clock.advance(7)
    w.launcher.complete("c", ok(), w.clock)
    w.sched.tick()
    assert w.result("c")["ts"] == (START + timedelta(seconds=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
