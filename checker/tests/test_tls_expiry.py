"""tls_expiry. The certificates in tests/certs are throwaway self-signed ones (CN/SAN localhost and 127.0.0.1):
`valid` runs from 2026 to 2126, `expired` from 2020-01-01 to 2020-01-11. The live tests serve them from a local TLS
server and, where a trusted certificate is needed, make the check trust that one file."""

from __future__ import annotations

import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import parse

from anystatus.checks import tls_expiry
from anystatus.checks.tls_expiry import TlsExpiry, days_between, not_after_from_der
from anystatus.judge import judge
from anystatus.model import Failure, Observation, Reading
from anystatus.scheduler import execute
from anystatus.table import ConfigError

CERTS = Path(__file__).parent / "certs"
NOW = datetime(2026, 11, 11, 10, 30, tzinfo=UTC)
VALID_NOT_AFTER = datetime(2126, 1, 1, tzinfo=UTC)
EXPIRED_NOT_AFTER = datetime(2020, 1, 11, tzinfo=UTC)


def spec_for(tmp_path: Path, extra: str = "", host: str = "127.0.0.1"):  # type: ignore[no-untyped-def]
    text = f'[[check]]\nid = "tls"\ntype = "tls_expiry"\nhost = "{host}"\n{extra}\n'
    return parse(text, tmp_path).checks[0]


@contextmanager
def tls_server(cert: str) -> Iterator[int]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERTS / f"{cert}.pem", CERTS / f"{cert}.key")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                conn.settimeout(2)
                with context.wrap_socket(conn, server_side=True):
                    pass
            except OSError:
                conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield int(listener.getsockname()[1])
    finally:
        stop.set()
        listener.close()


def trust(monkeypatch: pytest.MonkeyPatch, cert: str) -> None:
    """Judge certificates against tests/certs/<cert>.pem instead of the system's CAs, and fix the clock."""
    monkeypatch.setattr(
        tls_expiry, "trusted_context", lambda: ssl.create_default_context(cafile=str(CERTS / f"{cert}.pem"))
    )
    monkeypatch.setattr(tls_expiry, "_now", lambda: NOW)


# the rules, without a network


def test_default_limits_are_21_and_7_days_below(tmp_path: Path) -> None:
    spec = spec_for(tmp_path)
    assert (spec.limits.warn, spec.limits.crit, spec.limits.worse) == (21, 7, "below")
    levels = {
        days: judge(spec, TlsExpiry("h", 443, "h").reading(days)).level
        for days in (60, 21.5, 21, 8, 7, 0, -3)
    }
    assert levels == {60: "ok", 21.5: "ok", 21: "warn", 8: "warn", 7: "crit", 0: "crit", -3: "crit"}


def test_limits_can_be_changed(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, "warn = 30\ncrit = 14")
    assert [judge(spec, TlsExpiry("h", 443, "h").reading(d)).level for d in (31, 30, 14)] == [
        "ok",
        "warn",
        "crit",
    ]
    spec = spec_for(tmp_path, "crit = 3")  # only one limit given: the other keeps its default
    assert (spec.limits.warn, spec.limits.crit) == (21, 3)


def test_the_reading_says_days_and_goes_negative_when_expired() -> None:
    check = TlsExpiry("www.example.org", 443, "www.example.org")
    assert check.reading(52.34) == Reading(
        value=52.3, unit="d", text="expires in 52.3 days", fields={"host": "www.example.org"}
    )
    assert check.reading(-2.5).value == -2.5
    assert check.reading(-2.5).text == "expired 2.5 days ago"


def test_days_between() -> None:
    assert days_between(datetime(2026, 11, 14, 22, 30, tzinfo=UTC), NOW) == 3.5


def test_notAfter_from_der() -> None:
    for name, expected in (("valid", VALID_NOT_AFTER), ("expired", EXPIRED_NOT_AFTER)):
        der = ssl.PEM_cert_to_DER_cert((CERTS / f"{name}.pem").read_text())
        assert not_after_from_der(der) == expected  # 2020 is a UTCTime, 2126 a GeneralizedTime
    with pytest.raises(ValueError, match="certificate"):
        not_after_from_der(b"\x30\x03\x02\x01")
    with pytest.raises(ValueError, match="certificate"):
        not_after_from_der(ssl.PEM_cert_to_DER_cert((CERTS / "valid.pem").read_text())[:60])


def verification_error(code: int, message: str) -> ssl.SSLCertVerificationError:
    exc = ssl.SSLCertVerificationError(1, message)
    exc.verify_code = code
    exc.verify_message = message
    return exc


@pytest.mark.parametrize(
    ("code", "message", "summary"),
    [
        (
            62,
            "Hostname mismatch, certificate is not valid for 'www.example.org'.",
            "not valid for www.example.org",
        ),
        (64, "IP address mismatch", "not valid for www.example.org"),
        (9, "certificate is not yet valid", "certificate not valid yet"),
        (18, "self-signed certificate", "not trusted (self-signed certificate)"),
        (
            20,
            "unable to get local issuer certificate",
            "not trusted (unable to get local issuer certificate)",
        ),
    ],
)
def test_classification_of_refused_certificates(code: int, message: str, summary: str) -> None:
    check = TlsExpiry("www.example.org", 443, "www.example.org")
    assert check.refused(verification_error(code, message), 1) == Observation(level="crit", summary=summary)


def test_servername_is_what_the_certificate_must_be_valid_for(tmp_path: Path) -> None:
    check = spec_for(tmp_path, 'servername = "www.example.org"', host="10.0.0.5").check
    assert isinstance(check, TlsExpiry)
    assert (check.host, check.port, check.servername) == ("10.0.0.5", 443, "www.example.org")
    refused = check.refused(verification_error(62, "Hostname mismatch"), 1)
    assert refused.summary == "not valid for www.example.org"


# live, against a local TLS server


def test_trusted_certificate_gives_the_days_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trust(monkeypatch, "valid")
    with tls_server("valid") as port:
        spec = spec_for(tmp_path, f'port = {port}\nservername = "localhost"')
        outcome = execute(spec)
    assert isinstance(outcome, Reading) and outcome.unit == "d"
    assert outcome.value == round(days_between(VALID_NOT_AFTER, NOW), 1)
    core = judge(spec, outcome)
    assert core.level == "ok"
    assert core.summary == f"expires in {outcome.value:g} days"


def test_certificate_for_another_name_is_crit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trust(monkeypatch, "valid")
    with tls_server("valid") as port:
        outcome = execute(spec_for(tmp_path, f'port = {port}\nservername = "other.example"'))
    assert outcome == Observation(level="crit", summary="not valid for other.example")


def test_untrusted_certificate_is_crit_with_a_short_reason(tmp_path: Path) -> None:
    with tls_server("valid") as port:  # the system's CAs don't know the self-signed certificate
        spec = spec_for(tmp_path, f'port = {port}\nservername = "localhost"')
        outcome = execute(spec)
    assert isinstance(outcome, Observation) and outcome.level == "crit"
    assert outcome.summary is not None and outcome.summary.startswith("not trusted (")
    assert len(outcome.summary) < 80
    assert judge(spec, outcome).level == "crit"


def test_expired_certificate_is_crit_and_says_how_long_ago(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust(monkeypatch, "expired")
    with tls_server("expired") as port:
        spec = spec_for(tmp_path, f'port = {port}\nservername = "localhost"')
        outcome = execute(spec)
    days = round(days_between(EXPIRED_NOT_AFTER, NOW), 1)
    assert outcome == Observation(level="crit", summary=f"expired {-days:.1f} days ago", value=days, unit="d")
    assert judge(spec, outcome).level == "crit"


def test_not_reaching_the_server_is_a_failure(tmp_path: Path) -> None:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    outcome = execute(spec_for(tmp_path, f"port = {port}"))
    assert isinstance(outcome, Failure) and outcome.summary == "connection refused"
    assert judge(spec_for(tmp_path), outcome).level == "unknown"


def test_a_server_that_does_not_speak_tls_is_a_failure(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve() -> None:
        conn, _ = listener.accept()
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        outcome = execute(spec_for(tmp_path, f"port = {listener.getsockname()[1]}"))
    finally:
        listener.close()
    assert isinstance(outcome, Failure) and outcome.summary in (
        "TLS handshake failed",
        "connection lost",
    )  # the latter where the OS aborts first


def test_dns_failure_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_such_name(*args: object, **kwargs: object) -> socket.socket:
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr(socket, "create_connection", no_such_name)
    assert execute(spec_for(tmp_path, host="www.example.org")).summary == "host name not found"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "missing 'host'"),
        ('host = "x.example"\nport = 0', "'port' must be between 1 and 65535"),
        ('host = "x.example"\nport = "443"', "'port' must be a whole number"),
        ('host = "x.example"\nservername = ""', "'servername' must be a non-empty string"),
        ('host = "x.example"\nwarn = 7\ncrit = 21', "limits are out of order"),
        ('host = "x.example"\nwarn = "3d"', "'warn' must be a number"),
        ('host = "x.example"\nhostname = "y"', "unknown key 'hostname'"),
    ],
)
def test_config_errors(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(f'[[check]]\nid = "tls"\ntype = "tls_expiry"\n{body}\n', tmp_path)


def test_port_defaults_to_443(tmp_path: Path) -> None:
    check = spec_for(tmp_path, host="www.example.org").check
    assert isinstance(check, TlsExpiry) and (check.port, check.servername) == (443, "www.example.org")
