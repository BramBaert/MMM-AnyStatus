"""The mqtt check against a real broker. Skipped unless ANYSTATUS_TEST_MQTT names one (host:port), for example:

    docker run -d --rm --name mosq -p 1883:1883 -v ${PWD}/tests/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro \
        eclipse-mosquitto:2.0.22
    $env:ANYSTATUS_TEST_MQTT = "localhost:1883"; uv run pytest tests/test_mqtt_broker.py
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from helpers import parse

from anystatus.checks.mqtt import MqttCheck
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.scheduler import StoreMemory, SystemClock

ADDRESS = os.environ.get("ANYSTATUS_TEST_MQTT")
pytestmark = pytest.mark.skipif(not ADDRESS, reason="set ANYSTATUS_TEST_MQTT=host:port to run against a broker")


def wait_for(condition: Callable[[], bool], seconds: float = 10.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def publisher(host: str, port: int) -> Any:
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(host, port)
    client.loop_start()
    return client


def test_heartbeat_levels_and_retained_against_a_real_broker(tmp_path: Path) -> None:
    assert ADDRESS
    host, _, port = ADDRESS.partition(":")
    run = uuid.uuid4().hex[:8]
    hb, plug, value = f"t/{run}/hb", f"t/{run}/plug", f"t/{run}/state"
    text = f"""
[mqtt.b]
host = "{host}"
port = {port}

[[check]]
id = "hb"
type = "mqtt"
broker = "b"
topic = "{hb}"
warn = "1m"
crit = "5m"

[[check]]
id = "plug"
type = "mqtt"
broker = "b"
topic = "{plug}"
levels = {{ Online = "ok", Offline = "crit" }}

[[check]]
id = "temp"
type = "mqtt"
broker = "b"
topic = "{value}"
json = "t"
"""
    config = parse(text, tmp_path)
    checks: dict[str, MqttCheck] = {s.id: s.check for s in config.checks}  # type: ignore[misc]
    pub = publisher(host, int(port))
    try:
        # retained messages exist before the checker subscribes
        pub.publish(plug, "Online", retain=True).wait_for_publish()
        pub.publish(value, '{"t": 21.5}', retain=True).wait_for_publish()
        pub.publish(hb, "old", retain=True).wait_for_publish()
        clock = SystemClock()
        for cid, check in checks.items():
            check.start(StoreMemory(cid, None, clock, None))
        broker = checks["hb"].broker
        wait_for(lambda: broker.connected)
        wait_for(lambda: checks["plug"]._payload is not None and checks["temp"]._payload is not None)
        wait_for(lambda: checks["hb"]._payload is not None)

        out = checks["plug"].run(10)
        assert isinstance(out, Observation) and out.level == "ok"
        out = checks["temp"].run(10)
        assert isinstance(out, Reading) and out.value == 21.5
        with pytest.raises(CheckFailure, match="no message yet"):  # the retained heartbeat does not count
            checks["hb"].run(10)

        pub.publish(hb, "tick")  # a live one does
        wait_for(lambda: checks["hb"]._time is not None)
        out = checks["hb"].run(10)
        assert isinstance(out, Reading) and 0 <= out.value < 5
        pub.publish(plug, "Offline").wait_for_publish()
        wait_for(lambda: checks["plug"]._payload == "Offline")
        out = checks["plug"].run(10)
        assert isinstance(out, Observation) and out.level == "crit"
    finally:
        for check in checks.values():
            check.stop()
        pub.loop_stop()
        pub.disconnect()
        for topic in (hb, plug, value):  # clear the retained messages again
            cleaner = publisher(host, int(port))
            cleaner.publish(topic, b"", retain=True).wait_for_publish()
            cleaner.loop_stop()
            cleaner.disconnect()
