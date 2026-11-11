from __future__ import annotations

import base64
import json
import ssl
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from helpers import parse

from anystatus.checks import http as http_check
from anystatus.judge import judge
from anystatus.model import Failure, Observation, Reading
from anystatus.scheduler import execute
from anystatus.table import ConfigError

NOW = datetime(2026, 11, 11, 10, 30, tzinfo=UTC)
CERTS = Path(__file__).parent / "certs"

DOCUMENT = {
    "jobs": {"photos": {"last_success": "2026-11-11T09:00:00Z", "count": 42}},
    "items": [{"age": 5}, {"age": 7.5}],
    "text": "hunter2-in-the-body",
    "numeric_string": "12.5",
    "flag": True,
    "naive": "2026-11-11 09:00:00",
    "offset": "2026-11-11T10:00:00+01:00",
    "unix": 1794387600,
}


class Handler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]]

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def reply(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.requests.append({"path": self.path, "headers": dict(self.headers.items())})
        route = self.path.split("?")[0]
        if route == "/json":
            self.reply(200, json.dumps(DOCUMENT).encode())
        elif route == "/ok":
            self.reply(200, b"fine", "text/plain")
        elif route == "/empty":
            self.reply(204)
        elif route == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif route == "/bad":
            self.reply(503, b"hunter2-in-an-error-page", "text/plain")
        elif route == "/text":
            self.reply(200, b"this is hunter2, not JSON", "text/plain")
        elif route == "/big":
            self.reply(200, b'{"a": "' + b"x" * 1_100_000 + b'"}')
        elif route == "/hang":
            time.sleep(2)
            self.reply(200, b"{}")
        else:
            self.reply(404)


def serve(scheme: str = "http") -> tuple[ThreadingHTTPServer, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    handler = type("H", (Handler,), {"requests": seen})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    if scheme == "https":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERTS / "valid.pem", CERTS / "valid.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    return server, seen


@pytest.fixture
def server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    srv, seen = serve()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_check, "_now", lambda: NOW)


def spec_for(tmp_path: Path, url: str, extra: str = "", env: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    text = f'[[check]]\nid = "h"\ntype = "http"\nurl = "{url}"\n{extra}\n'
    return parse(text, tmp_path, env).checks[0]


def run(spec):  # type: ignore[no-untyped-def]
    outcome = execute(spec)
    return outcome, judge(spec, outcome)


# status and response time


def test_without_json_the_value_is_the_response_time(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    outcome, core = run(spec_for(tmp_path, f"{url}/ok"))
    assert isinstance(outcome, Reading) and outcome.unit == "ms"
    assert core.level == "ok"
    assert core.summary == f"HTTP 200, {outcome.value:g} ms"


def test_limits_apply_to_the_response_time(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    assert run(spec_for(tmp_path, f"{url}/ok", "warn = 0.0001"))[1].level == "warn"


def test_any_2xx_is_fine_by_default(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    assert run(spec_for(tmp_path, f"{url}/empty"))[1].level == "ok"


def test_unexpected_status_is_crit_and_names_only_the_code(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    outcome, core = run(spec_for(tmp_path, f"{url}/bad"))
    assert outcome == Observation(level="crit", summary="HTTP 503")
    assert core.level == "crit"
    assert run(spec_for(tmp_path, f"{url}/missing"))[0] == Observation(level="crit", summary="HTTP 404")


def test_redirects_are_not_followed(server: tuple[str, list], tmp_path: Path) -> None:
    url, seen = server
    assert run(spec_for(tmp_path, f"{url}/redirect"))[0] == Observation(level="crit", summary="HTTP 301")
    assert [r["path"] for r in seen] == ["/redirect"]


def test_expect_status(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    assert run(spec_for(tmp_path, f"{url}/bad", "expect_status = [503]"))[1].level == "ok"
    assert run(spec_for(tmp_path, f"{url}/bad", "expect_status = 503"))[1].level == "ok"
    assert run(spec_for(tmp_path, f"{url}/ok", "expect_status = [204, 301]"))[0] == Observation(
        level="crit", summary="HTTP 200"
    )


def test_query_string_is_sent(server: tuple[str, list], tmp_path: Path) -> None:
    url, seen = server
    run(spec_for(tmp_path, f"{url}/ok?a=1&b=2"))
    assert seen[0]["path"] == "/ok?a=1&b=2"
    assert seen[0]["headers"]["User-Agent"] == "anystatus-checker"


# json


def test_json_number(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    outcome, core = run(
        spec_for(tmp_path, f"{url}/json", 'json = "jobs.photos.count"\nwarn = 40\ncrit = 100')
    )
    assert isinstance(outcome, Reading) and outcome.value == 42
    assert (core.level, core.value, core.summary) == ("warn", 42, "jobs.photos.count = 42")


def test_json_list_index_and_numeric_string(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    assert run(spec_for(tmp_path, f"{url}/json", 'json = "items.1.age"'))[1].value == 7.5
    assert run(spec_for(tmp_path, f"{url}/json", 'json = "numeric_string"'))[1].value == 12.5


@pytest.mark.parametrize(
    ("path", "summary"),
    [
        ("jobs.photos.nope", "'jobs.photos.nope' not found"),
        ("items.5.age", "'items.5.age' not found"),
        ("text", "'text' is not a number"),
        ("flag", "'flag' is not a number"),
        ("jobs", "'jobs' is not a number"),
    ],
)
def test_json_problems_are_failures(
    server: tuple[str, list], tmp_path: Path, path: str, summary: str
) -> None:
    url, _ = server
    outcome, core = run(spec_for(tmp_path, f"{url}/json", f'json = "{path}"'))
    assert outcome == Failure(summary)
    assert core.level == "unknown"


def test_not_json_is_a_failure_without_the_body_text(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    outcome, _ = run(spec_for(tmp_path, f"{url}/text", 'json = "a"'))
    assert outcome == Failure("the answer is not JSON")


@pytest.mark.parametrize(
    ("path", "expected_h"),
    [
        ("jobs.photos.last_success", 1.5),  # 09:00Z, 1.5 h before NOW
        ("offset", 1.5),  # 10:00+01:00 is 09:00Z
        ("unix", 1.5),
        ("naive", 1.5),  # no offset: UTC
    ],
)
def test_value_is_time(server: tuple[str, list], tmp_path: Path, path: str, expected_h: float) -> None:
    url, _ = server
    outcome, core = run(spec_for(tmp_path, f"{url}/json", f'json = "{path}"\nvalue_is = "time"'))
    assert isinstance(outcome, Reading) and outcome.seconds
    assert (core.value, core.unit) == (expected_h, "h")
    assert core.summary == f"{expected_h:.1f} h ago"


def test_value_is_time_naive_uses_the_timezone(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    extra = 'json = "naive"\nvalue_is = "time"\ntimezone = "Europe/Brussels"'
    assert run(spec_for(tmp_path, f"{url}/json", extra))[1].value == 2.5  # 09:00 Brussels = 08:00Z
    # a time that carries an offset ignores the timezone
    extra = 'json = "offset"\nvalue_is = "time"\ntimezone = "Europe/Brussels"'
    assert run(spec_for(tmp_path, f"{url}/json", extra))[1].value == 1.5


def test_time_limits_are_durations(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    extra = 'json = "jobs.photos.last_success"\nvalue_is = "time"\nwarn = "1h"\ncrit = "2h"\nsummary = "ran {age} ago"'
    core = run(spec_for(tmp_path, f"{url}/json", extra))[1]
    assert (core.level, core.summary) == ("warn", "ran 1.5 h ago")


@pytest.mark.parametrize("path", ["text", "jobs.photos.count", "flag"])
def test_value_that_is_not_a_time_is_a_failure(server: tuple[str, list], tmp_path: Path, path: str) -> None:
    url, _ = server
    outcome, _ = run(spec_for(tmp_path, f"{url}/json", f'json = "{path}"\nvalue_is = "time"'))
    if path == "jobs.photos.count":  # a number is Unix seconds: 1970, a very old time, but a time
        assert isinstance(outcome, Reading)
    else:
        assert outcome == Failure(f"'{path}' is not a time")


# credentials and headers


def test_basic_auth(server: tuple[str, list], tmp_path: Path) -> None:
    url, seen = server
    spec = spec_for(tmp_path, f"{url}/ok", 'username = "monitor"\npassword_env = "PW"', {"PW": "s3cret:pw"})
    run(spec)
    expected = base64.b64encode(b"monitor:s3cret:pw").decode()
    assert seen[0]["headers"]["Authorization"] == f"Basic {expected}"
    assert "s3cret:pw" in spec.secrets  # masked in logs


def test_bearer_token(server: tuple[str, list], tmp_path: Path) -> None:
    url, seen = server
    run(spec_for(tmp_path, f"{url}/ok", 'bearer_env = "TOKEN"', {"TOKEN": "abc123"}))
    assert seen[0]["headers"]["Authorization"] == "Bearer abc123"


def test_headers_plain_and_from_the_environment(server: tuple[str, list], tmp_path: Path) -> None:
    url, seen = server
    extra = '[check.headers]\nX-Plain = "visible"\nX-Api-Key_env = "KEY"'
    spec = spec_for(tmp_path, f"{url}/ok", extra, {"KEY": "k-9"})
    run(spec)
    assert seen[0]["headers"]["X-Plain"] == "visible"
    assert seen[0]["headers"]["X-Api-Key"] == "k-9"
    assert "k-9" in spec.secrets
    assert "X-Api-Key_env" not in seen[0]["headers"]


def test_missing_environment_variable_makes_the_check_unknown(tmp_path: Path) -> None:
    for extra in ('bearer_env = "NOPE"', '[check.headers]\nX-Key_env = "NOPE"'):
        spec = spec_for(tmp_path, "http://127.0.0.1:1/x", extra)
        assert spec.env_problem == "environment variable NOPE not set"
        assert execute(spec) == Failure("environment variable NOPE not set")


# failures never leak


def test_refused_connection_is_a_failure_without_credentials(tmp_path: Path) -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    extra = 'bearer_env = "TOKEN"\n[check.headers]\nX-Api-Key_env = "KEY"'
    spec = spec_for(
        tmp_path,
        f"http://127.0.0.1:{port}/api?token=urlsecret",
        extra,
        {"TOKEN": "tok-secret", "KEY": "key-secret"},
    )
    outcome = execute(spec)
    assert outcome == Failure("connection refused", outcome.detail)  # type: ignore[union-attr]
    for secret in ("tok-secret", "key-secret", "urlsecret", "/api"):
        assert secret not in outcome.summary  # type: ignore[union-attr]


def test_no_secret_or_body_text_in_any_summary(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    env = {"PW": "hunter2"}
    base = 'username = "monitor"\npassword_env = "PW"\n'
    for path, extra in (
        ("bad", ""),  # the error page says hunter2
        ("text", 'json = "a"'),  # so does a body that is not JSON
        ("json", 'json = "text"'),  # and a value that is not a number
        ("json", 'json = "text"\nvalue_is = "time"'),
        ("json", 'json = "nope"'),
    ):
        _, core = run(spec_for(tmp_path, f"{url}/{path}", base + extra, env))
        assert core.summary is not None and "hunter2" not in core.summary


def test_timeout_is_a_failure(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    spec = spec_for(tmp_path, f"{url}/hang", 'timeout = "1s"')
    started = time.monotonic()
    assert execute(spec).summary == "timed out"  # type: ignore[union-attr]
    assert time.monotonic() - started < 1.9


def test_body_is_capped(server: tuple[str, list], tmp_path: Path) -> None:
    url, _ = server
    assert run(spec_for(tmp_path, f"{url}/big", 'json = "a"'))[0] == Failure("the answer is larger than 1 MB")
    assert run(spec_for(tmp_path, f"{url}/big"))[1].level == "ok"  # without json the body isn't read


# TLS


def test_https_is_verified_and_verify_tls_false_is_a_warning(tmp_path: Path) -> None:
    srv, _ = serve("https")
    try:
        url = f"https://127.0.0.1:{srv.server_address[1]}/ok"
        outcome = execute(spec_for(tmp_path, url))
        assert outcome == Failure("certificate not trusted", outcome.detail)  # type: ignore[union-attr]
        loose = spec_for(tmp_path, url, "verify_tls = false")
        assert execute(loose).__class__ is Reading
        config = parse(f'[[check]]\nid = "h"\ntype = "http"\nurl = "{url}"\nverify_tls = false\n', tmp_path)
        assert any("verify_tls = false" in w for w in config.warnings)
    finally:
        srv.shutdown()
        srv.server_close()


def test_verify_tls_true_and_plain_http_have_no_warning(tmp_path: Path) -> None:
    for extra in ("", "verify_tls = true"):
        config = parse(f'[[check]]\nid = "h"\ntype = "http"\nurl = "https://x.example/"\n{extra}\n', tmp_path)
        assert config.warnings == ()
    config = parse(
        '[[check]]\nid = "h"\ntype = "http"\nurl = "http://x.example/"\nverify_tls = false\n', tmp_path
    )
    assert config.warnings == ()


# config errors


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "missing 'url'"),
        ('url = "ftp://x.example/"', "'url' must look like"),
        ('url = "x.example/status"', "'url' must look like"),
        ('url = "https://monitor:pw@x.example/"', "credentials in the URL are not allowed"),
        ('url = "https://monitor@x.example/"', "credentials in the URL are not allowed"),
        ('url = "https://x.example:99999/"', "'url' is not a valid URL"),
        ('url = "https://x.example/"\nusername = "m"', "'username' needs 'password_env'"),
        ('url = "https://x.example/"\npassword_env = "PW"', "'password_env' needs 'username'"),
        ('url = "https://x.example/"\nusername = "m"\npassword_env = "PW"\nbearer_env = "T"', "not both"),
        ('url = "https://x.example/"\npassword = "plain"', "unknown key 'password'"),
        ('url = "https://x.example/"\nexpect_status = "200"', "'expect_status' must be"),
        ('url = "https://x.example/"\nexpect_status = [200, 99]', "'expect_status' must be"),
        ('url = "https://x.example/"\nexpect_status = []', "'expect_status' must be"),
        ('url = "https://x.example/"\njson = "a..b"', "empty part"),
        ('url = "https://x.example/"\njson = 5', "'json' must be a non-empty string"),
        ('url = "https://x.example/"\nvalue_is = "time"', "needs 'json'"),
        ('url = "https://x.example/"\nvalue_is = "number"', "'value_is' needs 'json'"),
        ('url = "https://x.example/"\njson = "a"\nvalue_is = "date"', "'value_is' must be one of"),
        ('url = "https://x.example/"\njson = "a"\ntimezone = "Europe/Brussels"', "'timezone' only applies"),
        (
            'url = "https://x.example/"\njson = "a"\ntimezone = "Mars/Base"\nvalue_is = "time"',
            "unknown time zone",
        ),
        ('url = "https://x.example/"\njson = "a"\nwarn = "2h"', "durations are for value_is"),
        ('url = "https://x.example/"\nverify_tls = "no"', "'verify_tls' must be true or false"),
        ('url = "https://x.example/"\nheaders = "X-A: b"', "'headers' must be a table"),
        ('url = "https://x.example/"\nheaders = { X-A = 1 }', "'headers' must be a table"),
        ('url = "https://x.example/"\nheaders = { "Bad Name" = "x" }', "not a valid header name"),
        ('url = "https://x.example/"\nheaders = { A = "x\\ny" }', "must be on one line"),
        ('url = "https://x.example/"\nheaders = { Host = "x" }', "set by the checker itself"),
        ('url = "https://x.example/"\nbearer_env = "T"\nheaders = { Authorization = "x" }', "conflicts"),
    ],
)
def test_config_errors(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(f'[[check]]\nid = "h"\ntype = "http"\n{body}\n', tmp_path)
