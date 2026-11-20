"""`http`: one GET request. The value is the response time in ms, or values read from the JSON answer: a number, a time,
an age in seconds, or a value that `levels` names a level for. Several named values (`[check.values.<name>]`) each have
their own limits; the worst of them is the check's level.

Standard library only (http.client). Redirects are not followed: a 301 is an unexpected status, which is also what
keeps credentials from travelling to a host the config didn't name. Nothing from the request (URL, credentials,
headers) or the answer (body text) ever goes into a summary, except a value that the config's own `levels` names.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any
from urllib.parse import urlsplit

from anystatus.checks.base import Check, register
from anystatus.errors import describe_exception
from anystatus.jsonpath import check_path, dig, to_age, to_number
from anystatus.judge import level_for
from anystatus.model import LEVELS, CheckFailure, Limits, Observation, Reading
from anystatus.table import Table
from anystatus.timeutil import format_duration

MAX_BODY = 1_000_000  # bytes of the answer that are read; a bigger answer is a failure
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_FORBIDDEN_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}
_VALUE_IS = ("number", "time", "seconds")
_VALUE_KEYS = ("json", "value_is", "timezone", "levels", "null", "missing")
_RANK = {"ok": 0, "warn": 1, "crit": 2}


@dataclass(frozen=True)
class JsonValue:
    """One value from the JSON answer and how it is judged. `limits` is None for a check's single value: the judge
    applies the check's own limits to it."""

    path: str
    value_is: str
    zone: tzinfo
    levels: dict[str, str] | None
    null: str
    missing: str
    limits: Limits | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_headers(table: Table, has_auth: bool) -> dict[str, str]:
    """The `headers` table. A key ending in `_env` names an environment variable that holds the value."""
    raw = table.str_map("headers")
    headers: dict[str, str] = {}
    for key, value in (raw or {}).items():
        is_env = key.endswith("_env")
        name = key[:-4] if is_env else key
        if not _HEADER_NAME.match(name):
            table.error(f"'headers': {key!r} is not a valid header name")
            continue
        lowered = name.lower()
        if lowered in _FORBIDDEN_HEADERS:
            table.error(f"'headers': {name} is set by the checker itself")
            continue
        if lowered == "authorization" and has_auth:
            table.error("'headers': Authorization conflicts with 'username' / 'bearer_env'")
            continue
        if lowered in {h.lower() for h in headers}:
            table.error(f"'headers': {name} is given more than once")
            continue
        if is_env:
            resolved = table.env_value(value)
            if resolved is None:
                continue
            value = resolved
        if "\r" in value or "\n" in value:
            table.error(f"'headers': the value of {name} must be on one line")
            continue
        headers[name] = value
    return headers


def _parse_value(table: Table, named: bool) -> JsonValue | None:
    """The keys of one value: on the check itself (`named` False: `json` is optional, and the limits are the check's),
    or in a `[check.values.<name>]` table (`json` required, its own limits). None when there is no `json`."""
    path = table.req_str("json") if named else table.opt_str("json")
    if path and (problem := check_path(path)):
        table.error(f"'json': {problem}")
    value_is = table.choice("value_is", _VALUE_IS, "number")
    if table.has("timezone") and value_is != "time":
        table.error("'timezone' only applies with value_is = \"time\"")
    zone = table.timezone("timezone")
    levels = table.str_map("levels")
    null = table.choice("null", LEVELS, "unknown")
    missing = table.choice("missing", LEVELS, "unknown")
    has_limits = any(table.has(key) for key in ("warn", "crit", "worse"))
    if value_is == "number":
        for key in ("warn", "crit"):
            if isinstance(table.data.get(key), str):
                table.error(f'\'{key}\' must be a number (durations are for value_is = "time" or "seconds")')
    if not path:
        for key in _VALUE_KEYS[1:]:
            if table.has(key):
                table.error(f"'{key}' needs 'json': the value has to come from the answer")
        return None
    if levels is not None:
        if not levels:
            table.error("'levels' is empty")
        for value, level in levels.items():
            if level not in LEVELS:
                table.error(f"'levels': '{value}' maps to '{level}', which is not one of {', '.join(LEVELS)}")
        if table.has("value_is"):
            table.error("'levels' and 'value_is' cannot be used together")
        if has_limits:
            table.error("'levels' names the level itself: no 'warn', 'crit' or 'worse' with it")
    limits = table.limits((None, None), value_is != "number", "above") if named else None
    return JsonValue(path, value_is, zone, levels, null, missing, limits)


@register
class Http(Check):
    type_name = "http"
    duration_limits = True  # limits are durations for value_is = "time" or "seconds" (checked in _parse_value)
    placeholders = frozenset({"status", "ms"})

    def __init__(
        self,
        *,
        https: bool,
        host: str,
        port: int | None,
        target: str,
        headers: dict[str, str],
        expect: list[int] | None,
        single: JsonValue | None,
        values: dict[str, JsonValue],
        verify_tls: bool,
    ) -> None:
        self.https = https
        self.host = host
        self.port = port
        self.target = target
        self.headers = headers
        self.expect = expect
        self.single = single
        self.values = values
        self.verify_tls = verify_tls

    @classmethod
    def from_config(cls, table: Table) -> Http:
        url = table.req_str("url")
        https, host, port, target = False, "", None, "/"
        if url:
            try:
                parts = urlsplit(url)
                port = parts.port
            except ValueError:
                parts = urlsplit("")
                table.error("'url' is not a valid URL")
            if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
                table.error("'url' must look like https://host/path (http or https)")
            elif parts.username is not None or parts.password is not None:
                table.error("credentials in the URL are not allowed: use 'username' and 'password_env', or 'bearer_env'")
            else:
                https = parts.scheme.lower() == "https"
                host = parts.hostname
                target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

        username = table.opt_str("username")
        has_password = table.has("password_env")
        has_bearer = table.has("bearer_env")
        if username is not None and not has_password:
            table.error("'username' needs 'password_env'")
        if has_password and username is None:
            table.error("'password_env' needs 'username'")
        if username is not None and has_bearer:
            table.error("use either 'username' with 'password_env', or 'bearer_env', not both")
        headers = _parse_headers(table, has_auth=username is not None or has_bearer)
        password = table.env("password") if has_password else None
        bearer = table.env("bearer") if has_bearer else None
        if username is not None and password is not None:
            token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        elif bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"

        expect = table.int_list("expect_status", 100, 599)
        single = _parse_value(table, named=False)
        values: dict[str, JsonValue] = {}
        raw_values = table.raw("values")
        if raw_values is not None:
            if not isinstance(raw_values, dict) or not raw_values or not all(isinstance(v, dict) for v in raw_values.values()):
                table.error("'values' must be named tables, such as [check.values.age]")
            else:
                if single is not None or any(table.has(key) for key in (*_VALUE_KEYS, "warn", "crit", "worse")):
                    table.error("with 'values', 'json', its keys and the limits go in each value")
                for name, raw in raw_values.items():
                    sub = Table(raw, f"{table.where}, value '{name}'", table.ctx)
                    value = _parse_value(sub, named=True)
                    sub.finish()
                    if value is not None:
                        values[name] = value

        verify_tls = table.boolean("verify_tls", True)
        if not verify_tls and https:
            table.warning("verify_tls = false: the server's certificate is not checked")
        return cls(
            https=https,
            host=host,
            port=port,
            target=target,
            headers=headers,
            expect=expect,
            single=single,
            values=values,
            verify_tls=verify_tls,
        )

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if not self.https:
            return http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        context = ssl.create_default_context()
        if not self.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=context)

    def _status_ok(self, status: int) -> bool:
        return 200 <= status < 300 if self.expect is None else status in self.expect

    def run(self, timeout: float) -> Observation | Reading:
        reads_json = self.single is not None or bool(self.values)
        deadline = time.monotonic() + timeout
        started = time.perf_counter()
        conn = self._connection(timeout)
        try:
            conn.request("GET", self.target, headers=self._request_headers(reads_json))
            response = conn.getresponse()
            status = response.status
            if not self._status_ok(status):
                return Observation(level="crit", summary=f"HTTP {status}")
            body = _read_body(response, deadline) if reads_json else b""
        except CheckFailure:
            raise
        except ssl.SSLCertVerificationError as exc:
            raise CheckFailure("certificate not trusted", detail=f"{type(exc).__name__}: {exc}") from exc
        except ssl.SSLError as exc:
            raise CheckFailure("TLS error", detail=f"{type(exc).__name__}: {exc}") from exc
        except http.client.HTTPException as exc:
            raise CheckFailure("invalid HTTP answer", detail=type(exc).__name__) from exc
        except OSError as exc:
            raise CheckFailure(describe_exception(exc), detail=f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()
        ms = round((time.perf_counter() - started) * 1000, 1)
        fields = {"status": str(status), "ms": f"{ms:g}"}
        if not reads_json:
            return Reading(value=ms, unit="ms", text=f"HTTP {status}, {ms:g} ms", fields=fields)
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except ValueError as exc:  # bad UTF-8 or bad JSON; the text of the answer stays out of the summary
            raise CheckFailure("the answer is not JSON") from exc
        if self.single is not None:
            return _read_value(self.single, data, fields)
        return _worst_of(self.values, data)

    def _request_headers(self, reads_json: bool) -> dict[str, str]:
        headers = {
            "User-Agent": "anystatus-checker",
            "Accept": "application/json" if reads_json else "*/*",
        }
        headers.update(self.headers)
        return headers


def _level_key(value: Any) -> str | None:
    """A JSON value as a key of `levels`: text as it is, true or false, a number as written (5, 2.5). None for an
    object or a list."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return None


def _read_value(spec: JsonValue, data: Any, fields: dict[str, str]) -> Reading | Observation:
    """One value from the decoded answer: a Reading for the limits, or an Observation when `levels`, `null` or
    `missing` decide the level. CheckFailure when it can't be read (the API changed, so the check can't tell)."""
    path = spec.path
    try:
        value = dig(data, path)
    except LookupError as exc:
        if spec.missing == "unknown":
            raise CheckFailure(str(exc)) from exc
        return Observation(spec.missing, f"'{path}' not found")
    if value is None:
        if spec.null == "unknown":
            raise CheckFailure(f"'{path}' is null")
        return Observation(spec.null, f"'{path}' is null")
    if spec.levels is not None:
        key = _level_key(value)
        if key is None:
            raise CheckFailure(f"'{path}' is not a single value")
        level = spec.levels.get(key)
        if level is None:  # not a value the config wrote, so it stays out of the summary
            return Observation("unknown", f"'{path}' has a value that 'levels' doesn't name")
        return Observation(level, f"{path} = {key}", value=key)
    if spec.value_is == "time":
        try:
            seconds = to_age(value, _now(), spec.zone)
        except ValueError as exc:
            raise CheckFailure(f"'{path}' is not a time") from exc
        shown, unit = format_duration(seconds)
        return Reading(value=seconds, seconds=True, text=f"{shown:.1f} {unit} ago", fields=fields)
    try:
        number = to_number(value)
    except ValueError as exc:
        raise CheckFailure(f"'{path}' is not a number") from exc
    if spec.value_is == "seconds":
        shown, unit = format_duration(number)
        return Reading(value=number, seconds=True, text=f"{path} = {shown:.1f} {unit}", fields=fields)
    return Reading(value=number, text=f"{path} = {number:g}", fields=fields)


def _worst_of(values: dict[str, JsonValue], data: Any) -> Observation:
    """Each named value judged by its own limits; the worst one gives the check's level, value and unit (the first of
    the worst, in the config's order). The summary has a line per value that isn't ok, or per value when all are. A
    value that can't be read, or that is unknown, makes the run a failure."""
    judged: list[tuple[str, str, str, float | str | None, str | None]] = []  # name, level, text, value, unit
    for name, spec in values.items():
        try:
            outcome = _read_value(spec, data, {})
        except CheckFailure as exc:
            raise CheckFailure(f"{name}: {exc.summary}", exc.detail) from exc
        if isinstance(outcome, Observation):
            if outcome.level == "unknown":
                raise CheckFailure(f"{name}: {outcome.summary}")
            judged.append((name, outcome.level, outcome.summary or "", outcome.value, outcome.unit))
            continue
        assert spec.limits is not None
        level = level_for(outcome.value, spec.limits)
        shown, unit = format_duration(outcome.value) if outcome.seconds else (outcome.value, outcome.unit)
        judged.append((name, level, outcome.text, shown, unit))
    worst = max(judged, key=lambda item: _RANK[item[1]])
    lines = [item for item in judged if item[1] != "ok"] or judged
    return Observation(worst[1], "\n".join(f"{name}: {text}" for name, _, text, _, _ in lines), worst[3], worst[4])


def _read_body(response: http.client.HTTPResponse, deadline: float) -> bytes:
    """The answer, at most MAX_BODY bytes (more is a failure), and within the run's deadline."""
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError
        chunk = response.read(65536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_BODY:
            raise CheckFailure("the answer is larger than 1 MB")
        chunks.append(chunk)
