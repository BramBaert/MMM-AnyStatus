"""`mqtt`: a device or service that publishes a heartbeat or a state, read from an MQTT broker.

One connection per broker (`[mqtt.<name>]`), shared by every check that names it. paho runs the network loop in its
own thread and calls `Broker._on_message` there; `run` is called on a worker thread, so the state a message changes is
guarded by a lock. Retained messages never count as a heartbeat (docs/checker.md).
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anystatus.checks.base import Check, CheckMemory, register, register_section
from anystatus.jsonpath import check_path, dig, to_number, to_time
from anystatus.model import LEVELS, CheckFailure, Observation, Reading
from anystatus.table import Context, Table
from anystatus.timeutil import age_seconds, format_duration, format_time, parse_duration, parse_time

log = logging.getLogger("anystatus")

MAX_PAYLOAD = 4096  # characters kept of a payload


def _now() -> datetime:
    return datetime.now(UTC)


def paho_available() -> bool:
    try:
        importlib.import_module("paho.mqtt.client")
    except ImportError:
        return False
    return True


def _span(seconds: float) -> str:
    """'10 min', '3 h', '20 s'."""
    shown, unit = format_duration(seconds)
    return f"{shown:g} {unit}"


def _clip(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Broker:
    """One broker and the one connection to it. Created from the config; connects when its first check starts."""

    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        *,
        tls: bool = False,
        ca_file: str | None = None,
        username: str | None = None,
        password: str | None = None,
        client_id: str | None = None,
        env_problem: str | None = None,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.tls = tls
        self.ca_file = ca_file
        self.username = username
        self.password = password
        self.client_id = client_id
        self.env_problem = env_problem
        self._lock = threading.Lock()
        self._checks: list[MqttCheck] = []
        self._client: Any = None
        self._connected = False

    # state

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def topics(self) -> list[str]:
        with self._lock:
            return sorted({c.topic for c in self._checks})

    # lifecycle

    def attach(self, check: MqttCheck) -> None:
        """A check starts: remember it, and open the connection if it is the first."""
        with self._lock:
            self._checks.append(check)
            client = self._client
            connected = self._connected
        if client is None:
            self._open()
        elif connected:
            client.subscribe(check.topic, 0)

    def detach(self, check: MqttCheck) -> None:
        with self._lock:
            if check in self._checks:
                self._checks.remove(check)
            last = not self._checks
            client = self._client
            if last:
                self._client = None
                self._connected = False
        if last and client is not None:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception as exc:
                log.warning("broker %s: error while closing: %s", self.name, type(exc).__name__)

    def _make_client(self) -> Any:
        mqtt = importlib.import_module("paho.mqtt.client")
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id or "")
        if self.tls:
            client.tls_set(ca_certs=self.ca_file)  # None: the system's certificate authorities
        if self.username is not None:
            client.username_pw_set(self.username, self.password)
        client.reconnect_delay_set(min_delay=1, max_delay=60)  # automatic reconnect, with backoff
        return client

    def _open(self) -> None:
        client = self._make_client()
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        with self._lock:
            self._client = client
        client.connect_async(self.host, self.port, keepalive=60)
        client.loop_start()

    # paho callbacks (paho's thread)

    def _on_connect(
        self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None
    ) -> None:
        if getattr(reason_code, "is_failure", False):
            log.info("broker %s: connection refused (%s)", self.name, reason_code)
            return
        with self._lock:
            self._connected = True
        log.info("broker %s: connected", self.name)
        for topic in self.topics():  # resubscribe on every (re)connect: a clean session forgets them
            client.subscribe(topic, 0)

    def _on_disconnect(
        self, client: Any, userdata: Any, flags: Any = None, reason_code: Any = None, properties: Any = None
    ) -> None:
        with self._lock:
            was = self._connected
            self._connected = False
        if was:
            log.info("broker %s: connection lost, reconnecting", self.name)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        with self._lock:
            targets = [c for c in self._checks if c.topic == message.topic]
        for check in targets:
            try:
                check.handle_message(bytes(message.payload), bool(message.retain))
            except Exception as exc:  # never let one check's trouble stop the network thread
                log.warning("%s: cannot handle a message: %s", check.name, type(exc).__name__)


def _peek_limit(table: Table, key: str) -> float | None:
    """A limit, read again without reporting problems (the generic parser has already reported them)."""
    value = table.data.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return parse_duration(value)
    except ValueError:
        return None


@register_section("mqtt")
def parse_brokers(raw: Any, ctx: Context) -> dict[str, Broker]:
    brokers: dict[str, Broker] = {}
    if not isinstance(raw, dict):
        ctx.errors.append("[mqtt] must contain one table per broker: [mqtt.home]")
        return brokers
    for name, body in raw.items():
        where = f"[mqtt.{name}]"
        if not isinstance(body, dict):
            ctx.errors.append(f"{where} must be a table")
            continue
        t = Table(body, where, ctx)
        host = t.req_str("host")
        tls = t.boolean("tls", False)
        port = t.integer("port", 8883 if tls else 1883, maximum=65535)
        ca_file = t.opt_str("ca_file")
        username = t.opt_str("username")
        password = t.env("password")
        client_id = t.opt_str("client_id")
        t.finish()
        if ca_file is not None and not tls:
            t.error("'ca_file' needs tls = true")
        if ca_file is not None:
            path = Path(ca_file)
            ca_file = str(path if path.is_absolute() else t.base_dir / path)
        env_problem = None
        if t.missing_env:
            env_problem = f"environment variable {', '.join(dict.fromkeys(t.missing_env))} not set"
            t.warning(env_problem)
        if not paho_available():
            t.error(
                "mqtt needs the 'mqtt' extra, which is not installed: uv add \"mmm-anystatus-checker[mqtt]\""
            )
        brokers[name] = Broker(
            name,
            host,
            port,
            tls=tls,
            ca_file=ca_file,
            username=username,
            password=password,
            client_id=client_id,
            env_problem=env_problem,
        )
    return brokers


@register
class MqttCheck(Check):
    type_name = "mqtt"
    duration_limits = True  # heartbeat: limits are durations; value mode: plain numbers are fine too

    def __init__(
        self,
        broker: Broker,
        topic: str,
        *,
        json_path: str | None = None,
        levels: dict[str, str] | None = None,
        max_age: float | None = None,
        time_json: str | None = None,
        warn: float | None = None,
        crit: float | None = None,
        name: str = "mqtt",
        now: Callable[[], datetime] = _now,
    ) -> None:
        self.broker = broker
        self.topic = topic
        self.json_path = json_path
        self.levels = levels
        self.max_age = max_age
        self.time_json = time_json
        self.warn = warn
        self.crit = crit
        self.name = name
        self.now = now
        self._lock = threading.Lock()
        self._payload: str | None = None  # the last message's payload, retained or not
        self._time: datetime | None = None  # the last message that counts as a heartbeat
        self._started: datetime | None = None
        self._memory: CheckMemory | None = None

    @classmethod
    def from_config(cls, table: Table) -> MqttCheck:
        broker_name = table.req_str("broker")
        topic = table.req_str("topic")
        json_path = table.opt_str("json")
        levels = table.str_map("levels")
        max_age = table.duration("max_age")
        time_json = table.opt_str("time_json")

        brokers: dict[str, Broker] = table.ctx.sections.get("mqtt", {})
        broker = brokers.get(broker_name)
        if broker_name and broker is None:
            known = ", ".join(sorted(brokers)) or "none configured"
            table.error(
                f"unknown broker '{broker_name}' (known: {known}); add a [mqtt.{broker_name}] section"
            )
            broker = Broker(broker_name, "", 1883)
        elif broker is None:
            broker = Broker("", "", 1883)

        if topic and ("+" in topic or "#" in topic):
            table.error("wildcards are not supported yet: 'topic' must be one exact topic")
        if json_path is not None and levels is not None:
            table.error("'json' and 'levels' cannot be used together")
        for key, path in (("json", json_path), ("time_json", time_json)):
            if path is not None and (problem := check_path(path)):
                table.error(f"'{key}': {problem}")
        if levels is not None:
            if not levels:
                table.error("'levels' is empty")
            for payload, level in levels.items():
                if level not in LEVELS:
                    table.error(
                        f"'levels': '{payload}' maps to '{level}', which is not one of {', '.join(LEVELS)}"
                    )
        return cls(
            broker,
            topic,
            json_path=json_path,
            levels=levels,
            max_age=max_age,
            time_json=time_json,
            warn=_peek_limit(table, "warn"),
            crit=_peek_limit(table, "crit"),
            name=table.where,
        )

    @property
    def mode(self) -> str:
        """'levels', 'value' or 'heartbeat'. A `max_age` without `levels` or `json` means the payload itself is the
        number (value mode), because a heartbeat already is an age."""
        if self.levels is not None:
            return "levels"
        if self.json_path is not None or self.max_age is not None:
            return "value"
        return "heartbeat"

    # lifecycle

    def start(self, memory: CheckMemory) -> None:
        self._memory = memory
        self._started = self.now()
        self._restore(memory.data)
        self.broker.attach(self)

    def stop(self) -> None:
        self.broker.detach(self)

    def _restore(self, data: dict[str, Any] | None) -> None:
        if not data:
            return
        payload, moment = data.get("payload"), data.get("time")
        with self._lock:
            if isinstance(payload, str):
                self._payload = payload
            if isinstance(moment, str):
                with contextlib.suppress(ValueError):
                    self._time = parse_time(moment)

    # messages (paho's thread)

    def handle_message(self, raw: bytes, retained: bool) -> None:
        payload = raw.decode("utf-8", errors="replace")[:MAX_PAYLOAD]
        moment = self._message_time(payload, retained)
        with self._lock:
            self._payload = payload
            if moment is not None:
                self._time = moment
            data = {"payload": payload, "time": format_time(self._time) if self._time else None}
        if self._memory is not None:
            self._memory.save(data)

    def _message_time(self, payload: str, retained: bool) -> datetime | None:
        """When the message happened, for the heartbeat. A retained message has no time of its own: only `time_json`
        can give it one. A live message is as old as its arrival, unless `time_json` says otherwise."""
        if self.time_json is not None:
            try:
                return to_time(dig(json.loads(payload), self.time_json))
            except (ValueError, LookupError):
                pass
        return None if retained else self.now()

    # evaluation (worker thread)

    def run(self, timeout: float) -> Observation | Reading:
        if self.broker.env_problem:
            raise CheckFailure(self.broker.env_problem)
        if not self.broker.connected:
            raise CheckFailure(f"broker {self.broker.name} unreachable")
        now = self.now()
        if self._started is None:
            self._started = now
        with self._lock:
            payload, moment = self._payload, self._time
        waited = max(0.0, age_seconds(self._started, now))  # a lower bound of the age when nothing was seen
        mode = self.mode

        if mode == "heartbeat":
            if moment is None:
                return self._nothing_yet(waited)
            age = max(0.0, age_seconds(moment, now))
            return Reading(value=age, seconds=True, text=f"last message {_span(age)} ago")

        if payload is None:
            if self.max_age is not None and waited >= self.max_age:
                return Observation("crit", f"no message for over {_span(self.max_age)}")
            raise CheckFailure("no message yet")
        if self.max_age is not None:
            age = max(0.0, age_seconds(moment, now)) if moment is not None else waited
            if age > self.max_age:
                return Observation("crit", f"silent for {_span(age)}")

        if mode == "levels":
            assert self.levels is not None
            level = self.levels.get(payload.strip())
            if level is None:
                return Observation("unknown", f"unexpected payload '{_clip(payload)}'")
            return Observation(level, _clip(payload.strip()))

        value = self._value(payload)
        return Reading(value=value, text=f"{value:g}")

    def _nothing_yet(self, waited: float) -> Observation:
        """No heartbeat message was ever seen: the age is at least the time since the subscription started."""
        if self.crit is not None and waited >= self.crit:
            return Observation("crit", f"no message for over {_span(self.crit)}")
        if self.warn is not None and waited >= self.warn:
            return Observation("warn", f"no message for over {_span(self.warn)}")
        raise CheckFailure("no message yet")

    def _value(self, payload: str) -> float:
        try:
            decoded: Any = json.loads(payload)
        except ValueError as exc:
            raise CheckFailure(
                "the payload is not JSON" if self.json_path else "the payload is not a number"
            ) from exc
        if self.json_path is not None:
            try:
                decoded = dig(decoded, self.json_path)
            except LookupError as exc:
                raise CheckFailure(f"'{self.json_path}' not found in the payload") from exc
        try:
            return to_number(decoded)
        except ValueError as exc:
            where = f"'{self.json_path}'" if self.json_path else "the payload"
            raise CheckFailure(f"{where} is not a number") from exc
