"""From what a run observed to what it reports: apply the limits, the `summary` template, and `show_after`."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from string import Formatter
from typing import TYPE_CHECKING, Any

from anystatus.model import LEVELS, Core, Failure, Level, Observation, Outcome, Reading, Reported, ShowAfter, Streaks
from anystatus.timeutil import format_duration

if TYPE_CHECKING:
    from anystatus.config import CheckSpec, Limits

MAX_SUMMARY = 200


def level_for(value: float, limits: Limits) -> Level:
    """Reaching a limit counts: `value >= crit` is crit when higher is worse; `value <= crit` when lower is."""
    if limits.worse == "above":
        if limits.crit is not None and value >= limits.crit:
            return "crit"
        if limits.warn is not None and value >= limits.warn:
            return "warn"
    else:
        if limits.crit is not None and value <= limits.crit:
            return "crit"
        if limits.warn is not None and value <= limits.warn:
            return "warn"
    return "ok"


class _Fields(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 1] + "…"


def _number(value: float) -> str:
    return f"{round(value, 2):g}"


def judge(spec: CheckSpec, outcome: Outcome) -> Core:
    """The level and text a run reports. Failures are always `unknown`."""
    if isinstance(outcome, Failure):
        return Core("unknown", _clip(outcome.summary))

    fields = _Fields()
    if isinstance(outcome, Reading):
        value = outcome.value
        if not math.isfinite(value):
            return Core("unknown", "the check's value is not a finite number")
        level = level_for(value, spec.limits)
        fields.update(outcome.fields)
        shown: float
        unit = outcome.unit
        if outcome.seconds:
            shown, unit = format_duration(value)
            fields["age"] = f"{shown:.1f} {unit}"
            fields["value"] = f"{shown:.1f}"
        else:
            shown = value
            fields["value"] = _number(shown)
        fields["unit"] = unit or ""
        text: str | None = outcome.text
        reported_value: float | str | None = shown
    elif isinstance(outcome, Observation):
        if outcome.level not in LEVELS:
            return Core("unknown", f"the check returned an unknown level: {outcome.level!r}"[:80])
        level = outcome.level  # type: ignore[assignment]
        reported_value = outcome.value
        unit = outcome.unit
        if isinstance(reported_value, float) and not math.isfinite(reported_value):
            reported_value = None
        if reported_value is not None:
            fields["value"] = reported_value if isinstance(reported_value, str) else _number(reported_value)
        fields["unit"] = unit or ""
        text = outcome.summary
    else:  # pragma: no cover - Outcome is closed
        raise TypeError(type(outcome))

    if spec.summary is not None:
        text = Formatter().vformat(spec.summary, (), _as_mapping(fields))
    return Core(level, _clip(text), reported_value, unit)


def _as_mapping(fields: _Fields) -> Any:
    return fields


_RANK = {"ok": 0, "warn": 1, "crit": 2}


def lasted(since: datetime | None, wait: float, now: datetime) -> bool:
    """Has a streak that began at `since` lasted `wait` seconds at `now`? `wait` = inf ("never") never has."""
    return since is not None and not math.isinf(wait) and (now - since).total_seconds() >= wait


def step(prev: Reported | None, new: Core, ts: datetime, show_after: ShowAfter, started: datetime | None = None) -> Reported:
    """What a run reports, given what was reported before (`prev`, None: nothing known yet) and `show_after`.

    `started` is when the run started (default `ts`); streaks are measured between run starts, which are at least an
    `interval` apart, so a level never shows early and at most one interval late.

    - ok, warn, crit are ordered. A level shows once the runs have seen it *or worse* for its `show_after` time; until
      then the highest lower level whose time has passed is shown, with this run's summary and value. So getting
      better is always reported at once: the better level's streak is at least as long as the worse one's.
    - unknown (a failure) has no lower level: until its time has passed, the last shown result stays, with the new
      `ts`. A failure neither breaks nor extends the warn and crit streaks (the run saw nothing).
    - `since` stays while the shown level stays. A level that shows because it got worse starts at its streak (when
      the problem began, not when it was shown); one that shows because it got better starts at this run.
    """
    started = started or ts
    old = prev.streaks if prev is not None else Streaks()
    if new.level == "unknown":
        streaks = Streaks(old.warn, old.crit, old.unknown or started)
        if prev is not None and not lasted(streaks.unknown, show_after.unknown, started):
            return Reported(core=prev.core, ts=ts, since=prev.since, streaks=streaks)
        since = prev.since if prev is not None and prev.core.level == "unknown" else (streaks.unknown or started)
        return Reported(core=new, ts=ts, since=since, streaks=streaks)

    rank = _RANK[new.level]
    streaks = Streaks(
        warn=(old.warn or started) if rank >= 1 else None,
        crit=(old.crit or started) if rank >= 2 else None,
    )
    shown: Level = "ok"
    if rank >= 2 and lasted(streaks.crit, show_after.crit, started):
        shown = "crit"
    elif rank >= 1 and lasted(streaks.warn, show_after.warn, started):
        shown = "warn"

    if shown == new.level:
        core = new
    elif shown == "ok" and prev is not None and prev.core.level == "ok":
        core = prev.core  # a worse level is waiting: green stays as it was, like a pending change always did
    else:
        core = replace(new, level=shown)

    if prev is not None and prev.core.level == shown:
        since = prev.since
    elif shown != "ok" and (prev is None or prev.core.level not in _RANK or _RANK[shown] > _RANK[prev.core.level]):
        since = (streaks.crit if shown == "crit" else streaks.warn) or started
    else:
        since = started
    return Reported(core=core, ts=ts, since=since, streaks=streaks)
