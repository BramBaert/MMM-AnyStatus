"""The base of every check type, and the registry that maps `type = "..."` to a class.

A new check type is one new file in this package: subclass Check, decorate with @register, import it in
checks/__init__.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from anystatus.model import Observation, Passthrough, Reading, Worse
from anystatus.table import Context, Table


class CheckMemory(Protocol):
    """The check's own persistent data (the SQLite `extra` column), for types that keep more than a level."""

    data: dict[str, Any] | None  # what was stored, if it is still within the ttl (the continuity rule)

    def save(self, data: dict[str, Any]) -> None:
        """Store `data`. Thread-safe: may be called from the check's own listener thread."""


@dataclass(frozen=True)
class Traits:
    worse: Worse
    duration_limits: bool
    placeholders: frozenset[str]
    default_limits: tuple[float | None, float | None]


class Check(ABC):
    """One configured check. Built once from its config table; `run` is called on every interval in a worker thread."""

    type_name: ClassVar[str]
    #: which direction of the value is worse; the config's `worse` overrides it
    worse: ClassVar[Worse] = "above"
    #: the value is a duration in seconds, so `warn` and `crit` may be written as "26h"
    duration_limits: ClassVar[bool] = False
    #: placeholders the type adds to a `summary` template, besides {value}, {unit} and (for durations) {age}
    placeholders: ClassVar[frozenset[str]] = frozenset()
    #: limits that apply when the config gives none: (warn, crit)
    default_limits: ClassVar[tuple[float | None, float | None]] = (None, None)

    @classmethod
    def traits(cls, table: Table) -> Traits:
        """What the config loader needs to know before `from_config`. Most types answer with the class variables
        above; a type whose direction, units or default limits depend on its own keys (`disk`, `system`: the
        `measure`) overrides this and peeks at `table.data` (validation is left to `from_config`)."""
        return Traits(cls.worse, cls.duration_limits, cls.placeholders, cls.default_limits)

    @classmethod
    @abstractmethod
    def from_config(cls, table: Table) -> Check:
        """Read the type's own keys from `table` (recording problems with table.error), and build the check."""

    def start(self, memory: CheckMemory) -> None:  # noqa: B027
        """Optional lifecycle: called once when the service starts, before any run. A type with a long-lived listener
        (a shared broker connection) starts it here, and restores what it kept from `memory.data`."""

    def stop(self) -> None:  # noqa: B027
        """Optional lifecycle: called once on shutdown."""

    @abstractmethod
    def run(self, timeout: float) -> Observation | Reading | Passthrough:
        """Do the check. May block. Raise CheckFailure when it can't find out. Must give up within `timeout` seconds
        where it can; a thread that doesn't is simply not started again (see docs/checker.md, Scheduling).
        Only `result_file` returns a Passthrough (results that are served as they are, see model.Passthrough)."""


REGISTRY: dict[str, type[Check]] = {}


def register(cls: type[Check]) -> type[Check]:
    REGISTRY[cls.type_name] = cls
    return cls


#: Top-level config sections owned by a check type (e.g. [mqtt.<name>]): name -> parser(raw value, context) -> the parsed
#: section, which checks read through `table.ctx.sections[name]`. Parsers record problems on ctx.errors.
SECTIONS: dict[str, Callable[[Any, Context], Any]] = {}


def register_section(name: str) -> Callable[[Callable[[Any, Context], Any]], Callable[[Any, Context], Any]]:
    def decorate(parser: Callable[[Any, Context], Any]) -> Callable[[Any, Context], Any]:
        SECTIONS[name] = parser
        return parser

    return decorate
