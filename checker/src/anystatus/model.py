"""What a check hands back, and the pieces the rest of the checker passes around."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Level = Literal["ok", "warn", "crit", "unknown"]
LEVELS: tuple[str, ...] = ("ok", "warn", "crit", "unknown")
Worse = Literal["above", "below"]


@dataclass(frozen=True)
class Observation:
    """A check that decides its own level (a custom function, a Nagios plugin, a Docker status)."""

    level: str
    summary: str | None = None
    value: float | str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class Reading:
    """A check that measures a number; the limits turn it into a level.

    With `seconds=True` the value is a duration in seconds: compared as such, reported in the largest unit that fits.
    `text` is the type's own summary; `fields` are the type's own placeholders for a `summary` template.
    """

    value: float
    unit: str | None = None
    text: str = ""
    fields: Mapping[str, str] = field(default_factory=dict)
    seconds: bool = False


@dataclass(frozen=True)
class Failure:
    """The check could not find out. Always `unknown`. `summary` must be free of secrets; `detail` goes to the log."""

    summary: str
    detail: str | None = None


class CheckFailure(Exception):
    """Raised by a check type when it can't find out. `summary` is short and secret-free (see Failure)."""

    def __init__(self, summary: str, detail: str | None = None) -> None:
        super().__init__(summary)
        self.summary = summary
        self.detail = detail


Outcome = Observation | Reading | Failure


@dataclass(frozen=True)
class Core:
    """What a run says, once the limits are applied: the part of a result that the debounce keeps or replaces."""

    level: Level
    summary: str | None = None
    value: float | str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class Reported:
    """The last reported (confirmed) result of a check, plus the debounce streak if a change is pending."""

    core: Core
    ts: datetime  # when the run that made this finished
    since: datetime  # when the reported level started
    cand_level: Level | None = None
    cand_count: int = 0
    cand_since: datetime | None = None


ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
