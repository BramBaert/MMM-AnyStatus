"""The room step 4 needs (an `mqtt` check type): lifecycle, type-specific data in SQLite, config sections owned by a type."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from helpers import FakeClock, make_world, parse

from anystatus.checks.base import Check, CheckMemory, register, register_section
from anystatus.model import Observation, Reading
from anystatus.store import Store
from anystatus.table import Context, Table

EVENTS: list[str] = []


@register_section("brokers")
def parse_brokers(raw: Any, ctx: Context) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, body in (raw or {}).items():
        t = Table(body, f"[brokers.{name}]", ctx)
        out[name] = t.req_str("host")
        t.finish()
    return out


@register
class Listener(Check):
    type_name = "listener_test"

    def __init__(self, broker: str) -> None:
        self.broker = broker
        self.memory: CheckMemory | None = None

    @classmethod
    def from_config(cls, table: Table) -> Listener:
        name = table.req_str("broker")
        brokers = table.ctx.sections.get("brokers", {})
        if name not in brokers:
            table.error(f"unknown broker '{name}'")
        return cls(brokers.get(name, ""))

    def start(self, memory: CheckMemory) -> None:
        self.memory = memory
        EVENTS.append(f"start restored={memory.data}")

    def stop(self) -> None:
        EVENTS.append("stop")

    def run(self, timeout: float) -> Observation | Reading:
        return Observation(level="ok")


CFG = """
[brokers.home]
host = "broker.example"
[[check]]
id = "l"
type = "listener_test"
broker = "home"
interval = "1m"
"""


def test_a_type_can_own_a_top_level_section_and_checks_can_read_it(tmp_path: Path) -> None:
    config = parse(CFG, tmp_path)
    assert config.sections["brokers"] == {"home": "broker.example"}
    assert config.checks[0].check.broker == "broker.example"  # type: ignore[attr-defined]


def test_section_problems_are_listed_with_the_rest(tmp_path: Path) -> None:
    from anystatus.table import ConfigError

    bad = CFG.replace('host = "broker.example"', 'hostt = "x"').replace('"home"', '"away"')
    try:
        parse(bad, tmp_path)
    except ConfigError as exc:
        text = "\n".join(exc.problems)
        assert "missing 'host'" in text
        assert "unknown key 'hostt'" in text
        assert "unknown broker 'away'" in text
    else:
        raise AssertionError("expected a ConfigError")


def test_start_and_stop_are_called_and_data_survives_a_restart_within_ttl(tmp_path: Path) -> None:
    EVENTS.clear()
    store = Store(tmp_path / "m.sqlite")
    clock = FakeClock()
    w = make_world(CFG, tmp_path, store=store, clock=clock)
    assert EVENTS == ["start restored=None"]
    check = w.config.checks[0].check
    check.memory.save({"last_message": "2026-11-11T12:00:00Z", "payload": "on"})  # type: ignore[attr-defined]
    w.sched.stop()
    assert EVENTS[-1] == "stop"

    clock2 = FakeClock(clock.now() + timedelta(seconds=100))  # within the ttl (3 min): continuity
    make_world(CFG, tmp_path, store=store, clock=clock2)
    assert EVENTS[-1] == "start restored={'last_message': '2026-11-11T12:00:00Z', 'payload': 'on'}"

    clock3 = FakeClock(clock.now() + timedelta(seconds=1000))  # past the ttl: nobody knows what happened
    make_world(CFG, tmp_path, store=store, clock=clock3)
    assert EVENTS[-1] == "start restored=None"


def test_extra_data_does_not_disturb_the_result_row(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.sqlite")
    w = make_world(CFG, tmp_path, store=store)
    w.config.checks[0].check.memory.save({"a": 1})  # type: ignore[attr-defined]
    assert store.load_all() == {}  # a row with data but no result yet is not a result
    w.run(Observation(level="ok"), check_id="l")
    assert "l" in store.load_all()
    assert store.load_extra()["l"][0] == {"a": 1}  # saving the result did not wipe it
