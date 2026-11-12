"""`docker_status`: are the containers that should run running, healthy and not restarting in a loop?

Reads only the container list (GET /containers/json) and Docker's events (GET /events), never a container's details
(GET /containers/{id}/json): those hold the container's whole environment, so usually its passwords, and a socket
proxy that allows paths rather than whole sections can then refuse them (docs/checker.md, "Docker access").

The check decides its own level (so `warn`, `crit`, `worse` are not accepted): crit for a listed container that is
missing or not running and for an `unhealthy` one, warn for more than `restarts` restarts within `restart_window`.
The value is the number of containers that aren't fine.

Health comes from the list's `Health` (Docker 29), or else from its status text ("Up 3 hours (unhealthy)"). Restarts
are a container's `die` events. Each run asks for the events since the previous run, a short stretch well within the
few hundred recent events the daemon keeps, and the times are kept in the check's memory for the window, so the
window survives a restart of the checker within the continuity rule. Without that memory (a first start, `once`)
the run asks for the whole window, as far back as the daemon still has events.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from anystatus.checks.base import Check, CheckMemory, register
from anystatus.checks.docker_api import DockerClient, Endpoint, docker_endpoint
from anystatus.model import CheckFailure, Observation
from anystatus.table import Table
from anystatus.timeutil import format_duration

DEFAULT_RESTARTS = 3
DEFAULT_WINDOW = 3600.0
MAX_NAMES = 6  # containers named in a summary; the rest is counted
DIE_EVENTS = json.dumps({"type": ["container"], "event": ["die"]})


def window_text(seconds: float) -> str:
    value, unit = format_duration(seconds)
    return f"{value:g} {unit}"


def health_of(item: dict[str, Any]) -> str | None:
    """`healthy`, `unhealthy`, `starting`, `none` or None, from a list entry."""
    health = item.get("Health")
    if isinstance(health, dict) and isinstance(health.get("Status"), str):
        return str(health["Status"])
    status = item.get("Status")
    if isinstance(status, str):
        for text, value in (("(unhealthy)", "unhealthy"), ("(health: starting)", "starting"), ("(healthy)", "healthy")):
            if text in status:
                return value
    return None


@register
class DockerStatus(Check):
    type_name = "docker_status"

    def __init__(
        self,
        endpoint: Endpoint,
        containers: list[str] | None,
        labels: list[str] | None,
        restarts: int,
        window: float,
    ) -> None:
        self.client = DockerClient(endpoint)
        self.containers = containers
        self.labels = labels
        self.restarts = restarts
        self.window = window
        self._now = time.time  # tests replace it
        self._dies: dict[str, list[float]] = {}  # container name -> times of its `die` events within the window
        self._until: int | None = None  # where the last run's question about events ended (whole seconds)
        self._newest: int = 0  # timeNano of the newest event recorded, so a boundary second isn't counted twice
        self._memory: CheckMemory | None = None

    @classmethod
    def from_config(cls, table: Table) -> DockerStatus:
        for key in ("warn", "crit", "worse"):
            if table.has(key):
                table.error(f"docker_status decides its own level (crit: missing, not running or unhealthy; warn: restarting): '{key}' is not used")
        endpoint = docker_endpoint(table)
        names = table.str_list("containers")
        labels = table.str_list("labels")
        if names is not None and labels is not None:
            table.error("use either 'containers' or 'labels', not both")
        if names is not None and not names:
            table.error("'containers' is empty")
        if labels is not None and not labels:
            table.error("'labels' is empty")
        restarts = table.integer("restarts", DEFAULT_RESTARTS, minimum=0)
        window = table.duration("restart_window", DEFAULT_WINDOW) or DEFAULT_WINDOW
        return cls(endpoint, [n.lstrip("/") for n in names] if names else None, labels, restarts, window)

    # memory

    def start(self, memory: CheckMemory) -> None:
        self._memory = memory
        data = memory.data
        if not isinstance(data, dict):
            return
        try:
            dies = {str(name): [float(t) for t in times] for name, times in data["dies"].items()}
            until, newest = int(data["until"]), int(data["newest"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return  # an old or damaged memory: start as without one
        self._dies, self._until, self._newest = dies, until, newest

    def _save(self) -> None:
        if self._memory is not None and self._until is not None:
            self._memory.save({"dies": self._dies, "until": self._until, "newest": self._newest})

    # run

    def run(self, timeout: float) -> Observation:
        deadline = time.monotonic() + timeout

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckFailure(f"timed out after {timeout:g} s")
            return remaining

        now = self._now()
        problems: dict[str, tuple[int, list[str]]] = {}  # name -> (0 crit / 1 warn, what is wrong), in order found
        targets: list[tuple[str, dict[str, Any]]] = []  # (name, list entry) of the containers to look at

        if self.containers is not None:
            listing = _listing(self.client.get_json("/containers/json", {"all": "true"}, timeout=left()))
            by_name = {n: item for item in listing for n in _names(item)}
            for name in self.containers:
                item = by_name.get(name)
                if item is None:
                    problems[name] = (0, ["missing"])
                else:
                    targets.append((name, item))
        else:
            query = {"filters": json.dumps({"label": self.labels})} if self.labels else None
            for item in _listing(self.client.get_json("/containers/json", query, timeout=left())):
                names = _names(item)
                targets.append((names[0] if names else str(item.get("Id", ""))[:12], item))

        self._record_dies(now, left)

        for name, item in targets:
            state = item.get("State")
            wrong: list[str] = []
            level = 1
            if state != "running":
                wrong.append(f"not running ({state})" if isinstance(state, str) and state else "not running")
                level = 0
            if health_of(item) == "unhealthy":
                wrong.append("unhealthy")
                level = 0
            restarts = len(self._dies.get(name, []))
            if restarts > self.restarts:
                wrong.append(f"{restarts} restarts in {window_text(self.window)}")
            if wrong:
                problems[name] = (level, wrong)

        if not problems:
            noun = "container" if len(targets) == 1 else "containers"
            return Observation(level="ok", summary=f"{len(targets)} {noun} fine" if targets else "no containers", value=0)
        listed = {name: i for i, name in enumerate(self.containers or [])}
        # crit first; then in the order of the config (or, without a list, as the API listed them)
        ordered = sorted(problems.items(), key=lambda item: (item[1][0], listed.get(item[0], 0)))
        shown = [f"{name}: {', '.join(wrong)}" for name, (_, wrong) in ordered[:MAX_NAMES]]
        if len(ordered) > MAX_NAMES:
            shown.append(f"+{len(ordered) - MAX_NAMES} more")
        level_name = "crit" if ordered[0][1][0] == 0 else "warn"
        return Observation(level=level_name, summary="; ".join(shown), value=len(problems))

    def _record_dies(self, now: float, left: Callable[[], float]) -> None:
        """Add the `die` events since the last run (or of the whole window) and forget those older than the window."""
        start = now - self.window
        since = self._until if self._until is not None and self._until > start else int(start)
        until = int(now)
        events = self.client.get_json_stream("/events", {"since": str(since), "until": str(until), "filters": DIE_EVENTS}, timeout=left())
        newest = self._newest
        for event in events:
            if not isinstance(event, dict) or event.get("Action", event.get("status")) != "die":
                continue
            actor = event.get("Actor")
            attributes = actor.get("Attributes") if isinstance(actor, dict) else None
            name = attributes.get("name") if isinstance(attributes, dict) else None
            nano = event.get("timeNano")
            if not isinstance(name, str) or not isinstance(nano, int) or isinstance(nano, bool):
                continue
            if nano <= self._newest:
                continue  # already counted: the previous question ended in the same second
            self._dies.setdefault(name, []).append(nano / 1e9)
            newest = max(newest, nano)
        self._dies = {name: kept for name, times in self._dies.items() if (kept := [t for t in times if t > start])}
        self._until, self._newest = until, newest
        self._save()


def _listing(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise CheckFailure("unexpected answer from the Docker API")
    return [item for item in data if isinstance(item, dict)]


def _names(item: dict[str, Any]) -> list[str]:
    names = item.get("Names")
    return [n.lstrip("/") for n in names if isinstance(n, str)] if isinstance(names, list) else []
