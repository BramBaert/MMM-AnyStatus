"""`show_after`, `since` and `ts`: all with a fake clock, no sleeping. The check runs every minute from 12:00."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from helpers import START, World

from anystatus.model import Failure, Observation


def ok(summary: str = "fine", value: float | None = None) -> Observation:
    return Observation(level="ok", summary=summary, value=value)


def warn(summary: str = "meh") -> Observation:
    return Observation(level="warn", summary=summary)


def crit(summary: str = "broken") -> Observation:
    return Observation(level="crit", summary=summary)


def at(minutes: int) -> str:
    return (START + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def shown(r: dict[str, object]) -> tuple[object, object]:
    return r["level"], r.get("summary")


def test_first_run_is_reported_at_once(one_check: Callable[..., World]) -> None:
    w = one_check()
    assert w.result("c")["summary"] == "waiting for the first run"
    r = w.run(crit())
    assert r["level"] == "crit"
    assert r["since"] == at(0)


def test_the_first_run_waits_like_any_other(one_check: Callable[..., World]) -> None:
    """No special case: nothing was shown before, so the streak starts with the first run."""
    w = one_check('show_after = { warn = "2m", crit = "2m" }')
    assert shown(w.run(crit("no answer"))) == ("ok", "no answer")
    assert w.run(Failure("refused"))["level"] == "unknown"  # unknown has no time here: at once


def test_without_show_after_every_change_shows_at_once(one_check: Callable[..., World]) -> None:
    w = one_check()
    w.run(ok())
    assert w.run(crit())["level"] == "crit"
    assert w.run(Failure("refused"))["level"] == "unknown"
    r = w.run(ok())
    assert (r["level"], r["since"]) == ("ok", at(3))


def test_a_level_shows_once_it_has_lasted_its_time(one_check: Callable[..., World]) -> None:
    w = one_check('show_after = { warn = "3m" }')
    w.run(ok())  # 12:00
    r = w.run(warn("first bad run"))  # 12:01: the streak starts
    assert shown(r) == ("ok", "fine")  # waiting: green stays as it was...
    assert r["ts"] == at(1)  # ...with the new ts
    assert w.run(warn())["level"] == "ok"  # 12:02: lasted 1 min
    assert w.run(warn())["level"] == "ok"  # 12:03: 2 min
    r = w.run(warn("fourth bad run"))  # 12:04: 3 min, the first run at or after the time
    assert shown(r) == ("warn", "fourth bad run")
    assert (r["since"], r["ts"]) == (at(1), at(4))  # since: when the problem began, not when it showed


def test_amber_at_once_red_after_a_while(one_check: Callable[..., World]) -> None:
    """The design's example: a fixable CRITICAL with warn = 0d, crit = 7d. Here in minutes."""
    w = one_check('show_after = { warn = "0d", crit = "3m" }')
    w.run(ok())
    r = w.run(crit("1 critical"))  # 12:01
    assert shown(r) == ("warn", "1 critical")  # the highest lower level whose time has passed, with this run's text
    assert r["since"] == at(1)
    assert w.run(crit())["level"] == "warn"  # 12:02
    assert w.run(crit())["level"] == "warn"  # 12:03
    r = w.run(crit("1 critical"))  # 12:04
    assert shown(r) == ("crit", "1 critical")
    assert r["since"] == at(1)  # when the crit streak began


def test_getting_better_is_reported_at_once(one_check: Callable[..., World]) -> None:
    w = one_check('show_after = { crit = "1m" }')
    w.run(crit())  # 12:00
    assert w.run(crit())["level"] == "crit"  # 12:01
    r = w.run(ok())  # green is green: at once
    assert (r["level"], r["since"]) == ("ok", at(2))
    # from red down to amber: the warn-or-worse streak includes the red runs, so amber needs no new wait
    w2 = one_check('show_after = { warn = "2m" }')
    w2.run(crit())  # 12:00
    w2.run(crit())  # 12:01
    r = w2.run(warn("only amber now"))  # 12:02: at least amber since 12:00
    assert shown(r) == ("warn", "only amber now")
    assert r["since"] == at(2)  # it got this level now


def test_flapping_never_shows(one_check: Callable[..., World]) -> None:
    w = one_check('show_after = { warn = "2m", crit = "2m" }')
    w.run(ok())
    for _ in range(6):
        assert w.run(crit())["level"] == "ok"
        r = w.run(ok())
        assert (r["level"], r["since"]) == ("ok", at(0))


def test_a_streak_that_breaks_starts_again(one_check: Callable[..., World]) -> None:
    w = one_check('show_after = { warn = "1m", crit = "1m" }')
    w.run(ok())
    w.run(crit())  # 12:01
    w.run(ok())  # back: streak gone
    assert w.run(crit())["level"] == "ok"  # 12:03 starts again from zero
    assert w.run(crit())["since"] == at(3)


def test_one_failure_does_not_turn_the_card_grey(one_check: Callable[..., World]) -> None:
    w = one_check('show_after = { unknown = "2m" }')
    w.run(ok())
    assert shown(w.run(Failure("connection refused"))) == ("ok", "fine")  # 12:01
    assert w.run(Failure("connection refused"))["level"] == "ok"  # 12:02
    r = w.run(Failure("connection refused"))  # 12:03: failing for 2 min
    assert shown(r) == ("unknown", "connection refused")
    assert r["since"] == at(1)
    assert w.run(ok())["level"] == "ok"  # better: at once


def test_a_failure_neither_breaks_nor_extends_a_streak(one_check: Callable[..., World]) -> None:
    w = one_check('show_after = { warn = "never", crit = "3m", unknown = "10m" }')
    w.run(ok())
    w.run(crit())  # 12:01
    w.run(Failure("timed out"))  # 12:02: saw nothing
    assert w.run(crit())["level"] == "ok"  # 12:03
    r = w.run(crit())  # 12:04: crit since 12:01
    assert (r["level"], r["since"]) == ("crit", at(1))


def test_never_skips_a_level(one_check: Callable[..., World]) -> None:
    w = one_check('show_after = { warn = "never", crit = "0d" }')
    w.run(ok())
    assert w.run(warn())["level"] == "ok"
    r = w.run(crit())
    assert r["level"] == "crit"
    r = w.run(warn("better"))  # down from red: amber is skipped, so green
    assert shown(r) == ("ok", "better")


def test_never_for_unknown_keeps_the_last_result(one_check: Callable[..., World]) -> None:
    w = one_check('show_after = { unknown = "never" }')
    w.run(warn())
    for _ in range(5):
        assert w.run(Failure("refused"))["level"] == "warn"


def test_shown_at_the_first_run_at_or_after_the_time(one_check: Callable[..., World]) -> None:
    """Times are measured between run starts, which are an interval apart: a run that takes a while to finish
    doesn't push the level one interval later, and nothing shows early."""
    w = one_check('show_after = { warn = "never", crit = "2m" }')
    for minute in range(4):
        w.sched.tick()  # starts at 12:0<minute>:00
        w.clock.advance(7)  # ...takes 7 s
        w.launcher.complete("c", ok() if minute == 0 else crit(), w.clock)
        w.sched.tick()
        w.clock.advance(53)
        r = w.result("c")
        assert r["level"] == ("crit" if minute == 3 else "ok"), minute  # started 12:01, shown at the 12:03 run
    assert r["since"] == at(1)


def test_a_change_of_value_or_summary_within_a_level_keeps_since(one_check: Callable[..., World]) -> None:
    w = one_check('show_after = { crit = "1m" }')
    w.run(ok("5 ms", 5))
    r = w.run(ok("7 ms", 7))
    assert (r["summary"], r["value"], r["since"]) == ("7 ms", 7, at(0))
    assert r["ts"] == at(1)


def test_while_amber_waits_for_red_the_text_is_the_latest(one_check: Callable[..., World]) -> None:
    w = one_check('show_after = { crit = "5m" }')
    w.run(warn("1 high"))
    r = w.run(crit("1 critical"))
    assert shown(r) == ("warn", "1 critical")
    assert r["since"] == at(0)  # still the same amber


def test_ts_is_when_the_run_finished(one_check: Callable[..., World]) -> None:
    w = one_check()
    w.sched.tick()
    w.clock.advance(7)
    w.launcher.complete("c", ok(), w.clock)
    w.sched.tick()
    assert w.result("c")["ts"] == (START + timedelta(seconds=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
