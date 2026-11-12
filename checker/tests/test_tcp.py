from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from helpers import parse

from anystatus.judge import judge
from anystatus.model import Failure, Observation, Reading
from anystatus.scheduler import execute
from anystatus.table import ConfigError


@contextmanager
def listening() -> Iterator[int]:
    with closing(socket.socket()) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        yield server.getsockname()[1]


def closed_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])  # nothing listens here once the socket is closed


def spec_for(tmp_path: Path, port: object, extra: str = "", host: str = "127.0.0.1"):  # type: ignore[no-untyped-def]
    text = f'[[check]]\nid = "t"\ntype = "tcp"\nhost = "{host}"\nport = {port}\ntimeout = "5s"\n{extra}\n'
    return parse(text, tmp_path).checks[0]


def test_open_port_gives_the_connect_time(tmp_path: Path) -> None:
    with listening() as port:
        spec = spec_for(tmp_path, port)
        outcome = execute(spec)
    assert isinstance(outcome, Reading)
    assert outcome.unit == "ms"
    assert 0 <= outcome.value < 1000
    core = judge(spec, outcome)
    assert core.level == "ok"
    assert core.summary == f"port {port} open, {outcome.value:g} ms"


def test_limits_apply_to_the_connect_time(tmp_path: Path) -> None:
    with listening() as port:
        # warn = 0: reached by any connect time. The value is rounded to 0.1 ms, so a fast localhost connect reads
        # 0.0 and a tiny positive limit would make this test depend on the machine's speed.
        spec = spec_for(tmp_path, port, "warn = 0")
        assert judge(spec, execute(spec)).level == "warn"
        spec = spec_for(tmp_path, port, "warn = 60000")  # a minute: never reached
        assert judge(spec, execute(spec)).level == "ok"


def test_refused_port_is_crit_not_a_failure(tmp_path: Path) -> None:
    port = closed_port()
    spec = spec_for(tmp_path, port)
    outcome = execute(spec)
    assert outcome == Observation(level="crit", summary=f"port {port} refused")
    assert judge(spec, outcome).level == "crit"


def test_no_answer_within_the_timeout_is_crit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def silent(*args: object, **kwargs: object) -> socket.socket:
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", silent)
    outcome = execute(spec_for(tmp_path, 3306))
    assert isinstance(outcome, Observation)
    assert (outcome.level, outcome.summary) == ("crit", "port 3306: no answer within 5 s")


def test_dns_failure_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_such_name(*args: object, **kwargs: object) -> socket.socket:
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr(socket, "create_connection", no_such_name)
    outcome = execute(spec_for(tmp_path, 3306, host="db.example"))
    assert outcome == Failure("host name not found", "[Errno 11001] getaddrinfo failed")
    assert judge(spec_for(tmp_path, 3306), outcome).level == "unknown"


def test_unreachable_network_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(*args: object, **kwargs: object) -> socket.socket:
        raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(socket, "create_connection", unreachable)
    outcome = execute(spec_for(tmp_path, 3306))
    assert isinstance(outcome, Failure) and outcome.summary == "cannot connect"


def test_summary_template(tmp_path: Path) -> None:
    with listening() as port:
        spec = spec_for(tmp_path, port, 'summary = "{host}:{port} answers in {value} {unit}"')
        assert judge(spec, execute(spec)).summary.startswith(f"127.0.0.1:{port} answers in ")  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('host = "db.example"', "missing 'port'"),
        ("port = 3306", "missing 'host'"),
        ('host = "db.example"\nport = 0', "'port' must be between 1 and 65535"),
        ('host = "db.example"\nport = 70000', "'port' must be between 1 and 65535"),
        ('host = "db.example"\nport = "3306"', "'port' must be a whole number"),
        ('host = "db.example"\nport = 3306\nprot = 1', "unknown key 'prot'"),
    ],
)
def test_config_errors(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(f'[[check]]\nid = "t"\ntype = "tcp"\n{body}\n', tmp_path)
