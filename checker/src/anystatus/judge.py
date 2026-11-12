"""From what a run observed to what it reports: apply the limits, the `summary` template, and the debounce."""

from __future__ import annotations

import math
from datetime import datetime
from string import Formatter
from typing import TYPE_CHECKING, Any

from anystatus.model import LEVELS, Core, Failure, Level, Observation, Outcome, Reading, Reported
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


def step(prev: Reported | None, new: Core, ts: datetime, confirm: int) -> Reported:
    """The debounce. `prev` is the last reported result and its pending streak (None: nothing known yet).

    A new level must be seen `confirm` runs in a row before it is reported. While it is pending, the last confirmed
    core (level, summary, value) is kept, with the new `ts`. When confirmed, `since` is where the streak started.
    """
    if prev is None:
        return Reported(core=new, ts=ts, since=ts)
    if new.level == prev.core.level:
        return Reported(core=new, ts=ts, since=prev.since)  # same level: the streak (if any) is broken
    if confirm <= 1:
        return Reported(core=new, ts=ts, since=ts)
    if prev.cand_level == new.level and prev.cand_since is not None:
        count, started = prev.cand_count + 1, prev.cand_since
    else:
        count, started = 1, ts
    if count >= confirm:
        return Reported(core=new, ts=ts, since=started)
    return Reported(core=prev.core, ts=ts, since=prev.since, cand_level=new.level, cand_count=count, cand_since=started)
