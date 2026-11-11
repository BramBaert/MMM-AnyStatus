"""Running checks on their intervals. The clock and the way a run is launched are injected, so the rules (spread,
start-to-start intervals, never overlapping, timeouts, a hung thread is not restarted, debounce, continuity after a
restart) are tested without sleeping.

All scheduler state is touched from one thread only (the one that calls tick). Worker threads only run the check and
hand back an outcome.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from anystatus.document import Snapshot, render_result
from anystatus.errors import describe_exception, mask
from anystatus.judge import judge, step
from anystatus.model import CheckFailure, Core, Failure, Outcome, Reported
from anystatus.store import Store

if TYPE_CHECKING:
    from collections.abc import Sequence

    from anystatus.config import CheckSpec

log = logging.getLogger("anystatus")

START_SPREAD = 5.0  # seconds over which the first runs are spread
# A run counts as hung this long after its own timeout: a check that honours its timeout reports its own, more
# telling failure ("query timed out") before the scheduler gives up on it.
HUNG_GRACE = 2.0
WAITING = "waiting for the first run"


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now(self) -> datetime: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)


class Handle(Protocol):
    """A run that was started. `done()` stays False for as long as its thread is alive."""

    finished_at: datetime | None
    outcome: Outcome | None

    def done(self) -> bool: ...


class Launcher(Protocol):
    def launch(self, spec: CheckSpec) -> Handle: ...


def execute(spec: CheckSpec) -> Outcome:
    """Run one check, in the calling thread, and turn whatever happens into an outcome."""
    if spec.env_problem:
        return Failure(spec.env_problem)
    try:
        return spec.check.run(spec.timeout)
    except CheckFailure as exc:
        return Failure(exc.summary, exc.detail)
    except Exception as exc:
        return Failure(describe_exception(exc), f"{type(exc).__name__}: {exc}")


class ThreadHandle:
    def __init__(self, spec: CheckSpec, clock: Clock, wake: threading.Event | None) -> None:
        self.finished_at: datetime | None = None
        self.outcome: Outcome | None = None
        self._done = threading.Event()
        self._clock = clock
        self._wake = wake
        self._spec = spec
        self._thread = threading.Thread(target=self._main, name=f"check-{spec.id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _main(self) -> None:
        try:
            outcome = execute(self._spec)
        except (
            BaseException
        ) as exc:  # execute handles Exception; this is for the rest, so the handle always ends
            outcome = Failure(describe_exception(exc))
        self.outcome = outcome
        self.finished_at = self._clock.now()
        self._done.set()
        if self._wake is not None:
            self._wake.set()

    def done(self) -> bool:
        return self._done.is_set()


class ThreadLauncher:
    """One daemon thread per run. A thread that never returns is left alone (it can't be killed) and, because
    the scheduler does not start a check while its thread is alive, it cannot pile up."""

    def __init__(self, clock: Clock, wake: threading.Event | None = None) -> None:
        self.clock = clock
        self.wake = wake

    def launch(self, spec: CheckSpec) -> Handle:
        handle = ThreadHandle(spec, self.clock, self.wake)
        handle.start()
        return handle


class StoreMemory:
    """Per-check persistent data for check types (see CheckMemory). Without a store it only lives in memory."""

    def __init__(self, check_id: str, store: Store | None, clock: Clock, data: dict[str, Any] | None) -> None:
        self.check_id = check_id
        self.store = store
        self.clock = clock
        self.data = data

    def save(self, data: dict[str, Any]) -> None:
        self.data = data
        if self.store:
            try:
                self.store.save_extra(self.check_id, data, self.clock.now())
            except sqlite3.Error as exc:
                log.error("%s: cannot write the memory file: %s", self.check_id, type(exc).__name__)


@dataclass
class Slot:
    spec: CheckSpec
    next_due: float
    state: Reported | None = None  # what the debounce continues from (None: start fresh)
    served: Reported | None = None  # what is reported now
    handle: Handle | None = None
    started: float = 0.0
    zombie: Handle | None = None  # a timed-out run whose thread is still alive
    runs: int = 0


class Scheduler:
    def __init__(
        self,
        specs: Sequence[CheckSpec],
        *,
        clock: Clock,
        launcher: Launcher,
        snapshot: Snapshot,
        store: Store | None = None,
        spread: float = START_SPREAD,
    ) -> None:
        self.clock = clock
        self.launcher = launcher
        self.snapshot = snapshot
        self.store = store
        self.spread = spread
        self.specs = list(specs)
        self.slots: list[Slot] = []

    def start(self) -> None:
        """Load the memory, publish the first snapshot, and set the start times (spread over the first seconds)."""
        mono = self.clock.monotonic()
        wall = self.clock.now()
        rows = self.store.load_all() if self.store else {}
        if self.store:
            for gone in self.store.prune([s.id for s in self.specs]):
                log.info("%s: no longer configured, forgotten", gone)
        extras = self.store.load_extra() if self.store else {}
        n = max(len(self.specs), 1)
        for index, spec in enumerate(self.specs):
            slot = Slot(spec, next_due=mono + self.spread * index / n)
            row = rows.get(spec.id)
            if row is None:
                slot.served = Reported(Core("unknown", WAITING), ts=wall, since=wall)
            else:
                slot.served = row
                if (wall - row.ts).total_seconds() <= spec.ttl:
                    slot.state = row
                else:
                    # Down longer than the ttl: nobody knows what happened in between. The old result is still
                    # served (honest: its ts is old); since and the debounce streak start fresh with the next run.
                    log.info(
                        "%s: stored result is older than its ttl, starting without since and streak", spec.id
                    )
            self.slots.append(slot)
            self._start_check(spec, extras.get(spec.id), wall)
        self._publish()

    def stop(self) -> None:
        """Tell the check types that listen all the time (see Check.start) to stop."""
        for spec in self.specs:
            try:
                spec.check.stop()
            except Exception as exc:
                log.warning("%s: error while stopping: %s", spec.id, type(exc).__name__)

    def tick(self) -> None:
        mono = self.clock.monotonic()
        changed = False
        for slot in self.slots:
            if slot.zombie is not None and slot.zombie.done():
                slot.zombie = None  # the thread ended; its late result is discarded
            handle = slot.handle
            if handle is not None:
                if handle.done():
                    self._finish(
                        slot, handle.outcome or Failure("no outcome"), handle.finished_at or self.clock.now()
                    )
                    slot.handle = None
                    changed = True
                elif mono - slot.started >= slot.spec.timeout + HUNG_GRACE:
                    slot.zombie, slot.handle = handle, None
                    self._finish(slot, Failure(f"timed out after {slot.spec.timeout:g} s"), self.clock.now())
                    changed = True
            if slot.handle is None and slot.zombie is None and mono >= slot.next_due:
                slot.handle = self.launcher.launch(slot.spec)
                slot.started = mono
                slot.next_due = mono + slot.spec.interval  # start to start
        if changed:
            self._publish()

    def next_wakeup(self, cap: float = 1.0) -> float:
        """Seconds until tick has something to do (at most `cap`)."""
        mono = self.clock.monotonic()
        wait = cap
        for slot in self.slots:
            if slot.handle is not None:
                wait = min(wait, slot.started + slot.spec.timeout + HUNG_GRACE - mono)
            elif slot.zombie is None:
                wait = min(wait, slot.next_due - mono)
        return max(0.0, wait)

    def first_runs_done(self) -> bool:
        return all(slot.runs >= 1 for slot in self.slots)

    def results(self) -> list[dict[str, Any]]:
        return [render_result(s.spec, s.served) for s in self.slots if s.served is not None]

    def run_forever(self, stop: threading.Event, wake: threading.Event) -> None:
        while not stop.is_set():
            self.tick()
            wake.wait(self.next_wakeup())
            wake.clear()

    # internals

    def _start_check(
        self, spec: CheckSpec, stored: tuple[dict[str, Any], datetime] | None, wall: datetime
    ) -> None:
        data = None
        if stored is not None and (wall - stored[1]).total_seconds() <= spec.ttl:  # the continuity rule
            data = stored[0]
        try:
            spec.check.start(StoreMemory(spec.id, self.store, self.clock, data))
        except Exception as exc:
            log.error("%s: cannot start: %s", spec.id, type(exc).__name__)

    def _finish(self, slot: Slot, outcome: Outcome, ts: datetime) -> None:
        spec = slot.spec
        previous = slot.served.core.level if slot.served else None
        core = judge(spec, outcome)
        reported = step(slot.state, core, ts, spec.confirm)
        slot.state = slot.served = reported
        slot.runs += 1

        if isinstance(outcome, Failure):
            log.info("%s: failed: %s", spec.id, mask(outcome.summary, spec.secrets))
            if outcome.detail:
                log.debug("%s: %s", spec.id, mask(outcome.detail, spec.secrets))
        log.debug("%s: run finished: %s %s", spec.id, core.level, core.summary or "")
        if previous != reported.core.level:
            log.info(
                "%s: %s -> %s%s",
                spec.id,
                previous or "?",
                reported.core.level,
                f" ({reported.core.summary})" if reported.core.summary else "",
            )
        if self.store:
            try:
                self.store.save(spec.id, json.dumps(render_result(spec, reported)), reported, ts)
            except sqlite3.Error as exc:
                log.error("%s: cannot write the memory file: %s", spec.id, type(exc).__name__)

    def _publish(self) -> None:
        self.snapshot.replace(self.results())
