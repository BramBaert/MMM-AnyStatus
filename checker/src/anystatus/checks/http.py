"""`http`: one GET request. The value is the response time in ms, or (with `json`) a number or a time from the answer.

Standard library only (http.client). Redirects are not followed: a 301 is an unexpected status, which is also what
keeps credentials from travelling to a host the config didn't name. Nothing from the request (URL, credentials,
headers) or the answer (body text) ever goes into a summary.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import ssl
import time
from datetime import UTC, datetime, tzinfo
from urllib.parse import urlsplit

from anystatus.checks.base import Check, register
from anystatus.errors import describe_exception
from anystatus.jsonpath import check_path, dig, to_age, to_number
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.table import Table
from anystatus.timeutil import format_duration

MAX_BODY = 1_000_000  # bytes of the answer that are read; a bigger answer is a failure
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_FORBIDDEN_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}


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


@register
class Http(Check):
    type_name = "http"
    duration_limits = True  # limits are durations when value_is = "time" (checked in from_config)
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
        json_path: str | None,
        value_is: str,
        zone: tzinfo,
        verify_tls: bool,
    ) -> None:
        self.https = https
        self.host = host
        self.port = port
        self.target = target
        self.headers = headers
        self.expect = expect
        self.json_path = json_path
        self.value_is = value_is
        self.zone = zone
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
        json_path = table.opt_str("json")
        if json_path is not None and (problem := check_path(json_path)):
            table.error(f"'json': {problem}")
        value_is = table.choice("value_is", ("number", "time"), "number")
        if value_is == "time" and json_path is None:
            table.error("'value_is = \"time\"' needs 'json': the time has to come from the answer")
        if table.has("value_is") and json_path is None and value_is == "number":
            table.error("'value_is' needs 'json'")
        if table.has("timezone") and value_is != "time":
            table.error("'timezone' only applies with value_is = \"time\"")
        zone = table.timezone("timezone")
        if value_is != "time":
            for key in ("warn", "crit"):
                if isinstance(table.data.get(key), str):
                    table.error(f"'{key}' must be a number (durations are for value_is = \"time\")")

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
            json_path=json_path,
            value_is=value_is,
            zone=zone,
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
        deadline = time.monotonic() + timeout
        started = time.perf_counter()
        conn = self._connection(timeout)
        try:
            conn.request("GET", self.target, headers=self._request_headers())
            response = conn.getresponse()
            status = response.status
            if not self._status_ok(status):
                return Observation(level="crit", summary=f"HTTP {status}")
            body = _read_body(response, deadline) if self.json_path else b""
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
        if self.json_path is None:
            return Reading(value=ms, unit="ms", text=f"HTTP {status}, {ms:g} ms", fields=fields)
        return self._from_json(body, fields)

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "anystatus-checker",
            "Accept": "application/json" if self.json_path else "*/*",
        }
        headers.update(self.headers)
        return headers

    def _from_json(self, body: bytes, fields: dict[str, str]) -> Reading:
        assert self.json_path is not None
        path = self.json_path
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except ValueError as exc:  # bad UTF-8 or bad JSON; the text of the answer stays out of the summary
            raise CheckFailure("the answer is not JSON") from exc
        try:
            value = dig(data, path)
        except LookupError as exc:
            raise CheckFailure(str(exc)) from exc
        if self.value_is == "time":
            try:
                seconds = to_age(value, _now(), self.zone)
            except ValueError as exc:
                raise CheckFailure(f"'{path}' is not a time") from exc
            shown, unit = format_duration(seconds)
            return Reading(value=seconds, seconds=True, text=f"{shown:.1f} {unit} ago", fields=fields)
        try:
            number = to_number(value)
        except ValueError as exc:
            raise CheckFailure(f"'{path}' is not a number") from exc
        return Reading(value=number, text=f"{path} = {number:g}", fields=fields)


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
