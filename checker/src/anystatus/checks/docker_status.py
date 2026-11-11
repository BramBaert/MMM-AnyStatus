"""`docker_status`: are the containers that should run running, healthy and not restarting in a loop?

Reads the Docker API (GET only, see docker_api). The check decides its own level (so `warn`, `crit`, `worse` are not
accepted): crit for a listed container that is missing or not running and for an `unhealthy` one, warn for more than
`restarts` restarts within `restart_window`. The value is the number of containers that aren't fine.

The restart history (a RestartCount sample whenever the count changes) is kept in the check's memory, so the window
survives a restart of the checker within the continuity rule.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from anystatus.checks.base import Check, CheckMemory, register
from anystatus.checks.docker_api import (
    DockerClient,
    Endpoint,
    NotFound,
    docker_endpoint,
)
from anystatus.model import CheckFailure, Observation
from anystatus.table import Table
from anystatus.timeutil import format_duration

DEFAULT_RESTARTS = 3
DEFAULT_WINDOW = 3600.0
MAX_NAMES = 6  # containers named in a summary; the rest is counted


@dataclass
class _History:
    """RestartCount samples of one container: (time, count) at the first sighting and whenever the count changed."""

    container_id: str
    samples: list[tuple[float, int]] = field(default_factory=list)

    def growth(self, count: int, now: float, window: float) -> int:
        """Restarts within the window, and record this sighting."""
        if self.samples and count < self.samples[-1][1]:
            self.samples = []  # the counter was reset (the container was recreated)
        if not self.samples or count != self.samples[-1][1]:
            self.samples.append((now, count))
        start = now - window
        baseline = self.samples[0][1]
        keep = 0
        for index, (moment, sampled) in enumerate(self.samples):
            if moment <= start:
                baseline, keep = sampled, index  # the latest sample at or before the window's start
        self.samples = self.samples[keep:]
        return count - baseline


def window_text(seconds: float) -> str:
    value, unit = format_duration(seconds)
    return f"{value:g} {unit}"


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
        self._history: dict[str, _History] = {}
        self._memory: CheckMemory | None = None

    @classmethod
    def from_config(cls, table: Table) -> DockerStatus:
        for key in ("warn", "crit", "worse"):
            if table.has(key):
                table.error(
                    f"docker_status decides its own level (crit: missing, not running or unhealthy; warn: "
                    f"restarting): '{key}' is not used"
                )
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
        saved = data.get("restarts") if isinstance(data, dict) else None
        if not isinstance(saved, dict):
            return
        for name, entry in saved.items():
            try:
                history = _History(
                    str(entry["id"]),
                    [(float(t), int(c)) for t, c in entry["samples"]],  # [[time, count], ...]
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._history[str(name)] = history

    def _save(self) -> None:
        if self._memory is not None:
            data: dict[str, Any] = {
                "restarts": {
                    name: {"id": h.container_id, "samples": [[t, c] for t, c in h.samples]}
                    for name, h in self._history.items()
                }
            }
            self._memory.save(data)

    # run

    def run(self, timeout: float) -> Observation:
        deadline = time.monotonic() + timeout

        def get(path: str, query: dict[str, str] | None = None) -> Any:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckFailure(f"timed out after {timeout:g} s")
            return self.client.get_json(path, query, timeout=remaining)

        now = self._now()
        problems: dict[
            str, tuple[int, list[str]]
        ] = {}  # name -> (0 crit / 1 warn, what is wrong), in order found
        targets: list[tuple[str, str]] = []  # (name, id) of the containers to look at

        if self.containers is not None:
            listing = _listing(get("/containers/json", {"all": "true"}))
            by_name = {n: item for item in listing for n in _names(item)}
            for name in self.containers:
                item = by_name.get(name)
                if item is None:
                    problems[name] = (0, ["missing"])
                else:
                    targets.append((name, str(item.get("Id", ""))))
        else:
            query = {"filters": json.dumps({"label": self.labels})} if self.labels else None
            for item in _listing(get("/containers/json", query)):
                names = _names(item)
                targets.append((names[0] if names else str(item.get("Id", ""))[:12], str(item.get("Id", ""))))

        histories: dict[str, _History] = {}
        for name, container_id in targets:
            try:
                info = get(f"/containers/{container_id}/json")
            except NotFound:
                if self.containers is not None:
                    problems[name] = (0, ["missing"])
                continue  # a container that went away between the two calls
            if not isinstance(info, dict):
                raise CheckFailure("unexpected answer from the Docker API")
            state = info.get("State")
            state = state if isinstance(state, dict) else {}
            status = state.get("Status")
            health = state.get("Health")
            health_status = health.get("Status") if isinstance(health, dict) else None
            wrong: list[str] = []
            level = 1
            if status != "running":
                wrong.append(
                    f"not running ({status})" if isinstance(status, str) and status else "not running"
                )
                level = 0
            if health_status == "unhealthy":
                wrong.append("unhealthy")
                level = 0
            count = info.get("RestartCount")
            if isinstance(count, int) and not isinstance(count, bool):
                history = self._history.get(name)
                if history is None or history.container_id != container_id:
                    history = _History(container_id)
                histories[name] = history
                grown = history.growth(count, now, self.window)
                if grown > self.restarts:
                    wrong.append(f"{grown} restarts in {window_text(self.window)}")
            if wrong:
                problems[name] = (level, wrong)
        self._history = histories
        self._save()

        if not problems:
            noun = "container" if len(targets) == 1 else "containers"
            return Observation(
                level="ok", summary=f"{len(targets)} {noun} fine" if targets else "no containers", value=0
            )
        listed = {name: i for i, name in enumerate(self.containers or [])}
        # crit first; then in the order of the config (or, without a list, as the API listed them)
        ordered = sorted(problems.items(), key=lambda item: (item[1][0], listed.get(item[0], 0)))
        shown = [f"{name}: {', '.join(wrong)}" for name, (_, wrong) in ordered[:MAX_NAMES]]
        if len(ordered) > MAX_NAMES:
            shown.append(f"+{len(ordered) - MAX_NAMES} more")
        level_name = "crit" if ordered[0][1][0] == 0 else "warn"
        return Observation(level=level_name, summary="; ".join(shown), value=len(problems))


def _listing(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise CheckFailure("unexpected answer from the Docker API")
    return [item for item in data if isinstance(item, dict)]


def _names(item: dict[str, Any]) -> list[str]:
    names = item.get("Names")
    return [n.lstrip("/") for n in names if isinstance(n, str)] if isinstance(names, list) else []
