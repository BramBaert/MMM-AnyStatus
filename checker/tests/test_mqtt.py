"""The mqtt check without a broker: messages are fed to the handler by hand, the clock is injected."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from helpers import FakeClock, make_world, parse

from anystatus.checks.mqtt import Broker, MqttCheck
from anystatus.model import CheckFailure, Failure, Observation, Reading
from anystatus.scheduler import StoreMemory
from anystatus.store import Store
from anystatus.table import ConfigError

BROKER = """
[mqtt.home]
host = "broker.example"
"""


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.calls: list[str] = []
        FakeClient.instances.append(self)

    def connect_async(self, host: str, port: int, keepalive: int = 60) -> None:
        self.calls.append(f"connect {host}:{port}")

    def loop_start(self) -> None:
        self.calls.append("loop_start")

    def loop_stop(self) -> None:
        self.calls.append("loop_stop")

    def disconnect(self) -> None:
        self.calls.append("disconnect")

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append(topic)


@pytest.fixture(autouse=True)
def fake_paho(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances = []
    monkeypatch.setattr(Broker, "_make_client", lambda self: FakeClient())


OK = SimpleNamespace(is_failure=False)


def connect(check: MqttCheck) -> FakeClient:
    client = FakeClient.instances[-1]
    check.broker._on_connect(client, None, None, OK)
    return client


def message(check: MqttCheck, payload: str, retained: bool = False, topic: str | None = None) -> None:
    msg = SimpleNamespace(topic=topic or check.topic, payload=payload.encode(), retain=retained)
    check.broker._on_message(None, None, msg)


class Rig:
    def __init__(self, text: str, tmp_path: Path, clock: FakeClock | None = None, store: Store | None = None):
        self.clock = clock or FakeClock()
        self.config = parse(BROKER + text, tmp_path)
        self.check: MqttCheck = self.config.checks[0].check  # type: ignore[assignment]
        self.check.now = self.clock.now
        self.check.broker.now = self.clock.now
        self.memory = StoreMemory(self.config.checks[0].id, store, self.clock, None)

    def start(self, data: dict[str, Any] | None = None) -> Rig:
        self.memory.data = data
        self.check.start(self.memory)
        connect(self.check)
        return self

    def run(self) -> Observation | Reading:
        return self.check.run(10)


def check_cfg(extra: str, topic: str = "boiler/heartbeat") -> str:
    return f"""
[[check]]
id = "c"
type = "mqtt"
broker = "home"
topic = "{topic}"
{extra}
"""


HEARTBEAT = check_cfg('warn = "3m"\ncrit = "10m"')


# heartbeat


def test_heartbeat_value_is_the_age_of_the_last_live_message(tmp_path: Path) -> None:
    rig = Rig(HEARTBEAT, tmp_path).start()
    message(rig.check, "tick")
    rig.clock.advance(125)
    out = rig.run()
    assert isinstance(out, Reading) and out.seconds
    assert out.value == 125
    assert "2.1 min ago" in out.text


def test_a_retained_message_is_not_a_heartbeat(tmp_path: Path) -> None:
    rig = Rig(HEARTBEAT, tmp_path).start()
    message(rig.check, "tick", retained=True)
    with pytest.raises(CheckFailure, match="no message yet"):
        rig.run()


def test_a_retained_message_with_time_json_does_count(tmp_path: Path) -> None:
    rig = Rig(check_cfg('time_json = "ts"\nwarn = "3m"\ncrit = "10m"'), tmp_path).start()
    message(rig.check, '{"ts": "2026-11-11T11:58:00Z"}', retained=True)  # two minutes before START
    out = rig.run()
    assert isinstance(out, Reading) and out.value == 120


def test_no_message_ever_is_unknown_until_a_limit_is_reached(tmp_path: Path) -> None:
    rig = Rig(HEARTBEAT, tmp_path).start()
    with pytest.raises(CheckFailure, match="no message yet"):
        rig.run()
    rig.clock.advance(180)
    out = rig.run()
    assert isinstance(out, Observation) and (out.level, out.summary) == ("warn", "no message for over 3 min")
    rig.clock.advance(420)
    out = rig.run()
    assert isinstance(out, Observation) and (out.level, out.summary) == ("crit", "no message for over 10 min")


def test_no_message_counts_only_from_the_latest_connect(tmp_path: Path) -> None:
    """A broker that comes up late (or reconnects) restarts the lower bound: messages sent while it was out of
    reach may have been missed, so they are no proof of silence."""
    rig = Rig(HEARTBEAT, tmp_path).start()
    rig.clock.advance(3600)  # an hour of listening without a message...
    connect(rig.check)  # ...then the connection drops and comes back
    with pytest.raises(CheckFailure, match="no message yet"):
        rig.run()
    rig.clock.advance(180)
    out = rig.run()
    assert isinstance(out, Observation) and out.level == "warn"


def test_no_message_without_limits_stays_unknown(tmp_path: Path) -> None:
    rig = Rig(check_cfg(""), tmp_path).start()
    rig.clock.advance(86400)
    with pytest.raises(CheckFailure, match="no message yet"):
        rig.run()


def test_a_message_after_a_silence_resets_the_age(tmp_path: Path) -> None:
    rig = Rig(HEARTBEAT, tmp_path).start()
    rig.clock.advance(700)
    message(rig.check, "tick")
    out = rig.run()
    assert isinstance(out, Reading) and out.value == 0


# persistence


def test_last_message_is_saved_and_restored(tmp_path: Path) -> None:
    store = Store(tmp_path / "m.sqlite")
    rig = Rig(HEARTBEAT, tmp_path, store=store).start()
    message(rig.check, "tick")
    saved = rig.memory.data
    assert saved is not None and saved["payload"] == "tick" and saved["time"] == "2026-11-11T12:00:00Z"

    rig2 = Rig(HEARTBEAT, tmp_path, clock=FakeClock(rig.clock.now() + timedelta(seconds=90))).start(saved)
    out = rig2.run()
    assert isinstance(out, Reading) and out.value == 90  # the age did not reset to zero


def test_nothing_restored_counts_as_no_message(tmp_path: Path) -> None:
    # Past the continuity rule the scheduler hands over data=None: the check starts as if it never saw a message.
    rig = Rig(HEARTBEAT, tmp_path).start(None)
    with pytest.raises(CheckFailure, match="no message yet"):
        rig.run()


def test_a_retained_message_keeps_the_saved_heartbeat_time(tmp_path: Path) -> None:
    rig = Rig(HEARTBEAT, tmp_path).start({"payload": "tick", "time": "2026-11-11T11:59:00Z"})
    message(rig.check, "tick", retained=True)
    out = rig.run()
    assert isinstance(out, Reading) and out.value == 60
    assert rig.memory.data == {"payload": "tick", "time": "2026-11-11T11:59:00Z"}


def test_garbage_in_the_memory_is_ignored(tmp_path: Path) -> None:
    rig = Rig(HEARTBEAT, tmp_path).start({"payload": 5, "time": "yesterday"})
    with pytest.raises(CheckFailure, match="no message yet"):
        rig.run()


# value


def test_value_from_json_uses_the_limits_and_retained_content(tmp_path: Path) -> None:
    rig = Rig(check_cfg('json = "sensors.0.temp"\nwarn = -15\ncrit = -10', "freezer/state"), tmp_path).start()
    message(rig.check, '{"sensors": [{"temp": -18.5}]}', retained=True)
    out = rig.run()
    assert isinstance(out, Reading) and out.value == -18.5 and not out.seconds


def test_plain_numeric_payload_with_max_age(tmp_path: Path) -> None:
    rig = Rig(check_cfg('max_age = "15m"\nwarn = 5'), tmp_path).start()
    message(rig.check, " 7.5\n")
    out = rig.run()
    assert isinstance(out, Reading) and out.value == 7.5


def test_max_age_makes_a_silent_value_crit(tmp_path: Path) -> None:
    rig = Rig(check_cfg('json = "t"\nmax_age = "15m"'), tmp_path).start()
    message(rig.check, '{"t": 1}')
    rig.clock.advance(1200)
    out = rig.run()
    assert isinstance(out, Observation) and (out.level, out.summary) == ("crit", "silent for 20 min")


def test_max_age_with_only_a_retained_message_counts_from_the_start(tmp_path: Path) -> None:
    rig = Rig(check_cfg('json = "t"\nmax_age = "15m"'), tmp_path).start()
    message(rig.check, '{"t": 1}', retained=True)
    assert isinstance(rig.run(), Reading)
    rig.clock.advance(1000)
    out = rig.run()
    assert isinstance(out, Observation) and out.level == "crit"


def test_value_mode_with_no_message(tmp_path: Path) -> None:
    rig = Rig(check_cfg('json = "t"\nmax_age = "15m"'), tmp_path).start()
    with pytest.raises(CheckFailure, match="no message yet"):
        rig.run()
    rig.clock.advance(900)
    out = rig.run()
    assert isinstance(out, Observation) and (out.level, out.summary) == ("crit", "no message for over 15 min")


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        ("not json", "not JSON"),
        ('{"x": 1}', "'t' not found"),
        ('{"t": "warm"}', "'t' is not a number"),
        ('{"t": true}', "'t' is not a number"),
    ],
)
def test_a_payload_in_another_format_is_a_failure(tmp_path: Path, payload: str, problem: str) -> None:
    rig = Rig(check_cfg('json = "t"'), tmp_path).start()
    message(rig.check, payload)
    with pytest.raises(CheckFailure, match=problem):
        rig.run()


def test_plain_payload_that_is_not_a_number(tmp_path: Path) -> None:
    rig = Rig(check_cfg('max_age = "1h"'), tmp_path).start()
    message(rig.check, "hello")
    with pytest.raises(CheckFailure, match="not a number"):
        rig.run()


# levels

LEVELS = check_cfg('levels = { Online = "ok", Offline = "crit" }', "plug/LWT")


def test_levels_map_the_payload(tmp_path: Path) -> None:
    rig = Rig(LEVELS, tmp_path).start()
    message(rig.check, "Online\n", retained=True)
    out = rig.run()
    assert isinstance(out, Observation) and out.level == "ok"
    message(rig.check, "Offline")
    out = rig.run()
    assert isinstance(out, Observation) and out.level == "crit"


def test_unexpected_payload_is_unknown_and_clipped(tmp_path: Path) -> None:
    rig = Rig(LEVELS, tmp_path).start()
    message(rig.check, "Rebooting")
    out = rig.run()
    assert isinstance(out, Observation) and (out.level, out.summary) == (
        "unknown",
        "unexpected payload 'Rebooting'",
    )
    message(rig.check, "x" * 500)
    out = rig.run()
    assert isinstance(out, Observation) and out.summary is not None and len(out.summary) < 70


def test_levels_with_max_age(tmp_path: Path) -> None:
    rig = Rig(check_cfg('levels = { Online = "ok" }\nmax_age = "1h"', "plug/LWT"), tmp_path).start()
    message(rig.check, "Online")
    rig.clock.advance(7200)
    out = rig.run()
    assert isinstance(out, Observation) and out.level == "crit" and out.summary == "silent for 2 h"


# broker


def test_broker_not_connected_is_a_failure(tmp_path: Path) -> None:
    rig = Rig(HEARTBEAT, tmp_path)
    rig.check.start(rig.memory)  # no on_connect yet
    with pytest.raises(CheckFailure, match="broker home unreachable"):
        rig.run()
    connect(rig.check)
    rig.check.broker._on_disconnect(None, None)
    with pytest.raises(CheckFailure, match="broker home unreachable"):
        rig.run()


def test_failure_is_debounced_by_confirm(tmp_path: Path) -> None:
    w = make_world(BROKER + check_cfg('interval = "1m"\nconfirm = 2'), tmp_path)
    check: MqttCheck = w.config.checks[0].check  # type: ignore[assignment]
    check.now = w.clock.now
    connect(check)
    message(check, "tick")
    assert w.run(check.run(10))["level"] == "ok"  # the first result is reported as is
    check.broker._on_disconnect(None, None)
    assert w.run(Failure("broker home unreachable"))["level"] != "unknown"  # one failure: still pending
    assert w.run(Failure("broker home unreachable"))["level"] == "unknown"


def test_one_connection_per_broker_and_resubscribe(tmp_path: Path) -> None:
    text = BROKER + check_cfg("", "a/b").replace('"c"', '"c1"') + check_cfg("", "c/d").replace('"c"', '"c2"')
    config = parse(text, tmp_path)
    checks: list[MqttCheck] = [s.check for s in config.checks]  # type: ignore[misc]
    for index, check in enumerate(checks):
        check.start(StoreMemory(f"c{index}", None, FakeClock(), None))
    assert len(FakeClient.instances) == 1
    client = FakeClient.instances[0]
    assert client.calls == ["connect broker.example:1883", "loop_start"]
    client.subscribed.clear()
    checks[0].broker._on_connect(client, None, None, OK)  # (re)connect: everything is subscribed again
    assert client.subscribed == ["a/b", "c/d"]
    client.subscribed.clear()
    checks[0].broker._on_connect(client, None, None, OK)
    assert client.subscribed == ["a/b", "c/d"]
    message(checks[0], "x")
    assert checks[0]._payload == "x" and checks[1]._payload is None  # routed by topic
    checks[0].stop()
    assert "disconnect" not in client.calls
    checks[1].stop()
    assert client.calls[-2:] == ["disconnect", "loop_stop"]


def test_a_refused_connection_is_not_connected(tmp_path: Path) -> None:
    rig = Rig(HEARTBEAT, tmp_path)
    rig.check.start(rig.memory)
    rig.check.broker._on_connect(FakeClient.instances[-1], None, None, SimpleNamespace(is_failure=True))
    assert not rig.check.broker.connected


# config


def bad(text: str, tmp_path: Path) -> str:
    with pytest.raises(ConfigError) as info:
        parse(text, tmp_path)
    return "\n".join(info.value.problems)


def test_broker_table_defaults_and_keys(tmp_path: Path) -> None:
    text = ('[mqtt.a]\nhost = "h"\n[mqtt.b]\nhost = "h"\ntls = true\nusername = "u"\npassword_env = "PW"\nclient_id = "x"\n' + check_cfg("")).replace(
        'broker = "home"', 'broker = "a"'
    )
    config = build_with_env(text, tmp_path, {"PW": "s3"})
    a, b = config.sections["mqtt"]["a"], config.sections["mqtt"]["b"]
    assert (a.port, a.tls) == (1883, False)
    assert (b.port, b.tls, b.username, b.password, b.client_id) == (8883, True, "u", "s3", "x")


def build_with_env(text: str, tmp_path: Path, env: dict[str, str]) -> Any:
    return parse(text, tmp_path, env)


def test_config_errors(tmp_path: Path) -> None:
    assert "unknown key 'hots'" in bad('[mqtt.home]\nhost = "h"\nhots = 1\n' + check_cfg(""), tmp_path)
    assert "missing 'host'" in bad("[mqtt.home]\n" + check_cfg(""), tmp_path)
    assert "'ca_file' needs tls" in bad('[mqtt.home]\nhost = "h"\nca_file = "ca.pem"\n' + check_cfg(""), tmp_path)
    assert "unknown broker 'away'" in bad(BROKER + check_cfg("").replace("home", "away"), tmp_path)
    assert "wildcards are not supported yet" in bad(BROKER + check_cfg("", "a/+/b"), tmp_path)
    assert "wildcards are not supported yet" in bad(BROKER + check_cfg("", "a/#"), tmp_path)
    assert "cannot be used together" in bad(BROKER + check_cfg('json = "a"\nlevels = { On = "ok" }'), tmp_path)
    assert "not one of" in bad(BROKER + check_cfg('levels = { On = "fine" }'), tmp_path)
    assert "'json': " in bad(BROKER + check_cfg('json = "a..b"'), tmp_path)
    assert "missing 'topic'" in bad(BROKER + check_cfg("").replace('topic = "boiler/heartbeat"', ""), tmp_path)
    assert "unknown key 'colour'" in bad(BROKER + check_cfg('colour = "red"'), tmp_path)


def test_missing_extra_is_a_config_error_naming_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("anystatus.checks.mqtt.paho_available", lambda: False)
    text = bad(BROKER + HEARTBEAT, tmp_path)
    assert "'mqtt' extra" in text and "mmm-anystatus-checker[mqtt]" in text


def test_missing_password_variable_makes_the_check_unknown(tmp_path: Path) -> None:
    rig = Rig('\n[mqtt.x]\nhost = "h"\npassword_env = "NOPE"\n' + HEARTBEAT.replace("home", "x"), tmp_path)
    rig.check.start(rig.memory)
    connect(rig.check)
    with pytest.raises(CheckFailure, match="environment variable NOPE not set"):
        rig.run()
