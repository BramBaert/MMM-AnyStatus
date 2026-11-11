"""Shared test helpers: a fake clock, a fake launcher, and a way to build a scheduler from TOML text."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from anystatus.checks.base import Check, register
from anystatus.config import Config, build_config
from anystatus.document import Snapshot
from anystatus.model import Observation, Outcome, Reading
from anystatus.scheduler import Handle, Scheduler
from anystatus.store import Store
from anystatus.table import Table

START = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)


@register
class Scripted(Check):
    """A check type for tests: its outcomes are fed in by the test, so it never runs anything itself."""

    type_name = "scripted"
    duration_limits = False

    @classmethod
    def from_config(cls, table: Table) -> Scripted:
        return cls()

    def run(self, timeout: float) -> Observation | Reading:
        raise AssertionError("Scripted checks are completed by the test")


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self.wall = start
        self.mono = 1000.0

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


@dataclass
class FakeHandle:
    finished_at: datetime | None = None
    outcome: Outcome | None = None
    _done: bool = False

    def done(self) -> bool:
        return self._done


@dataclass
class FakeLauncher:
    launched: dict[str, list[FakeHandle]] = field(default_factory=dict)
    times: list[tuple[str, float]] = field(default_factory=list)
    clock: FakeClock | None = None

    def launch(self, spec: Any) -> Handle:
        handle = FakeHandle()
        self.launched.setdefault(spec.id, []).append(handle)
        if self.clock:
            self.times.append((spec.id, self.clock.mono))
        return handle

    def count(self, check_id: str) -> int:
        return len(self.launched.get(check_id, []))

    def complete(self, check_id: str, outcome: Outcome, clock: FakeClock) -> None:
        handle = self.launched[check_id][-1]
        handle.outcome, handle.finished_at, handle._done = outcome, clock.now(), True


@dataclass
class World:
    config: Config
    clock: FakeClock
    launcher: FakeLauncher
    snapshot: Snapshot
    sched: Scheduler

    def result(self, check_id: str) -> dict[str, Any]:
        return next(r for r in self.sched.results() if r["id"] == check_id)

    def run(self, outcome: Outcome, check_id: str = "c", advance: float | None = None) -> dict[str, Any]:
        """One full run of a check: it starts, finishes with `outcome`, the next one becomes due."""
        self.sched.tick()
        assert self.launcher.launched[check_id][-1]._done is False, "the check was not started"
        self.launcher.complete(check_id, outcome, self.clock)
        self.sched.tick()
        spec = next(s for s in self.config.checks if s.id == check_id)
        self.clock.advance(spec.interval if advance is None else advance)
        return self.result(check_id)


def parse(text: str, tmp_path: Path, env: dict[str, str] | None = None) -> Config:
    return build_config(tomllib.loads(text), tmp_path, env or {})


def make_world(
    text: str,
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    store: Store | None = None,
    clock: FakeClock | None = None,
    spread: float = 0.0,
    start: bool = True,
) -> World:
    config = parse(text, tmp_path, env)
    clock = clock or FakeClock()
    launcher = FakeLauncher(clock=clock)
    snapshot = Snapshot(config.name)
    sched = Scheduler(
        config.checks, clock=clock, launcher=launcher, snapshot=snapshot, store=store, spread=spread
    )
    if start:
        sched.start()
    return World(config, clock, launcher, snapshot, sched)


ONE_CHECK = """
[[check]]
id = "c"
type = "scripted"
interval = "1m"
{extra}
"""


def read_toml(text: str) -> dict[str, Any]:
    return tomllib.loads(text)
