"""Functions for the `python` check tests (imported by name from the config)."""

import threading
from typing import Any

from anystatus import Observation

gate = threading.Event()
calls: list[str] = []


def number(cfg: dict[str, Any]) -> float:
    return float(cfg.get("n", 4))


def echo_config(cfg: dict[str, Any]) -> Observation:
    return Observation(level="ok", summary=repr(sorted(cfg.items())))


def decide(cfg: dict[str, Any]) -> Observation:
    return Observation(level="warn", summary="decided by me", value=3, unit="things")


async def async_number(cfg: dict[str, Any]) -> float:
    return 12.5


def boom(cfg: dict[str, Any]) -> float:
    raise ValueError("password=hunter2 is wrong")


def returns_none(cfg: dict[str, Any]) -> None:
    return None


def returns_bool(cfg: dict[str, Any]) -> bool:
    return True


def bad_level(cfg: dict[str, Any]) -> Observation:
    return Observation(level="purple", summary="?")


def blocker(cfg: dict[str, Any]) -> float:
    """Hangs until the test opens the gate: a thread that cannot be killed."""
    calls.append("start")
    gate.wait(timeout=30)
    return 1.0
