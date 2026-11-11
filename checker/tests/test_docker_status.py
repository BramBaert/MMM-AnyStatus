from __future__ import annotations

import http.client
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeDocker
from helpers import parse

from anystatus.checks.docker_api import DockerClient, is_windows, parse_endpoint, running_images
from anystatus.checks.docker_status import DockerStatus
from anystatus.judge import judge
from anystatus.model import Failure, Observation
from anystatus.scheduler import execute
from anystatus.table import ConfigError

T0 = 1_000_000.0


class Memory:
    """What the scheduler's StoreMemory is to a check: data restored at start, save() keeps it."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data

    def save(self, data: dict[str, Any]) -> None:
        self.data = data


@pytest.fixture
def docker() -> Iterator[FakeDocker]:
    with FakeDocker() as fake:
        yield fake


def build(tmp_path: Path, docker: FakeDocker, extra: str = "") -> Any:
    text = f'[[check]]\nid = "d"\ntype = "docker_status"\ndocker = "{docker.url}"\ninterval = "1m"\n{extra}\n'
    return parse(text, tmp_path).checks[0]


def run(spec: Any, now: float | None = None) -> Any:
    if now is not None:
        spec.check._now = lambda: now
    outcome = execute(spec)
    return outcome, judge(spec, outcome)


def test_all_running_containers_are_the_default_and_fine(tmp_path: Path, docker: FakeDocker) -> None:
    outcome, core = run(build(tmp_path, docker))
    assert outcome == Observation(level="ok", summary="6 containers fine", value=0)
    assert (core.level, core.value) == ("ok", 0)
    # only the running ones are asked for; every request is a GET
    assert ("GET", "/containers/json") in docker.requests
    assert docker.methods() == {"GET"}


def test_listed_containers_missing_or_not_running_are_crit(tmp_path: Path, docker: FakeDocker) -> None:
    spec = build(tmp_path, docker, 'containers = ["stack-demo-1", "db-old", "nope"]')
    _, core = run(spec)
    assert core.level == "crit" and core.value == 2
    assert core.summary == "db-old: not running (exited); nope: missing"


def test_a_listed_container_that_runs_is_fine(tmp_path: Path, docker: FakeDocker) -> None:
    _, core = run(build(tmp_path, docker, 'containers = ["stack-demo-1", "/stack-mosquitto-1"]'))
    assert core.level == "ok" and core.summary == "2 containers fine"


def test_unhealthy_is_crit_and_starting_is_fine(tmp_path: Path, docker: FakeDocker) -> None:
    docker.set_state("stack-demo-1", health="unhealthy")
    docker.set_state("stack-checker-1", health="starting")
    docker.set_state("stack-mosquitto-1", health="healthy")
    _, core = run(build(tmp_path, docker))
    assert (core.level, core.value, core.summary) == ("crit", 1, "stack-demo-1: unhealthy")


def test_default_ignores_stopped_containers_but_a_paused_listed_one_is_crit(
    tmp_path: Path, docker: FakeDocker
) -> None:
    docker.set_state("stack-demo-1", status="paused")
    _, core = run(build(tmp_path, docker, 'containers = ["stack-demo-1"]'))
    assert core.level == "crit" and core.summary == "stack-demo-1: not running (paused)"


def test_labels_select_containers(tmp_path: Path, docker: FakeDocker) -> None:
    docker.set_state("stack-demo-1", labels={"status.watch": "true"}, health="unhealthy")
    docker.set_state("stack-mosquitto-1", health="unhealthy")  # not watched
    _, core = run(build(tmp_path, docker, 'labels = ["status.watch=true"]'))
    assert (core.level, core.summary) == ("crit", "stack-demo-1: unhealthy")
    watched = [p for m, p in docker.requests if p.startswith("/containers/json")]
    assert any("filters=" in p and "status.watch" in p for p in watched)
    # the inspect calls only covered the watched container
    assert (
        len(
            [
                p
                for _, p in docker.requests
                if p.endswith("/json") and "/containers/" in p and "json?" not in p
            ]
        )
        == 1
    )


def test_no_matching_container_is_fine(tmp_path: Path, docker: FakeDocker) -> None:
    _, core = run(build(tmp_path, docker, 'labels = ["nothing=here"]'))
    assert (core.level, core.value, core.summary) == ("ok", 0, "no containers")


# --- restarts: growth within the window, from a history kept in the check's memory


def test_restart_growth_within_the_window_is_a_warning(tmp_path: Path, docker: FakeDocker) -> None:
    spec = build(tmp_path, docker, "restarts = 3")
    docker.set_state("stack-demo-1", restarts=40)  # restarted a lot before we ever looked: not growth
    assert run(spec, T0)[1].level == "ok"
    docker.set_state("stack-demo-1", restarts=43)  # 3 more: not more than 3
    assert run(spec, T0 + 600)[1].level == "ok"
    docker.set_state("stack-demo-1", restarts=45)  # 5 within the hour
    core = run(spec, T0 + 1200)[1]
    assert (core.level, core.value, core.summary) == ("warn", 1, "stack-demo-1: 5 restarts in 1 h")


def test_the_window_moves_on(tmp_path: Path, docker: FakeDocker) -> None:
    spec = build(tmp_path, docker, 'restarts = 3\nrestart_window = "30m"')
    docker.set_state("stack-demo-1", restarts=0)
    run(spec, T0)
    docker.set_state("stack-demo-1", restarts=5)
    core = run(spec, T0 + 300)[1]
    assert core.level == "warn" and core.summary == "stack-demo-1: 5 restarts in 30 min"
    assert run(spec, T0 + 1500)[1].level == "warn"  # still inside the window
    assert run(spec, T0 + 3000)[1].level == "ok"  # the restarts are older than 30 min now


def test_crit_wins_over_warn_and_both_are_named(tmp_path: Path, docker: FakeDocker) -> None:
    spec = build(tmp_path, docker, "restarts = 1")
    run(spec, T0)
    docker.set_state("stack-demo-1", restarts=5)
    docker.set_state("stack-mosquitto-1", health="unhealthy")
    core = run(spec, T0 + 60)[1]
    assert core.level == "crit" and core.value == 2
    assert core.summary == "stack-mosquitto-1: unhealthy; stack-demo-1: 5 restarts in 1 h"


def test_the_history_survives_a_restart_of_the_checker(tmp_path: Path, docker: FakeDocker) -> None:
    memory = Memory()
    first = build(tmp_path, docker, "restarts = 3").check
    first.start(memory)
    first._now = lambda: T0
    first.run(10)
    assert memory.data is not None  # saved after the run

    docker.set_state("stack-demo-1", restarts=6)
    again = build(
        tmp_path, docker, "restarts = 3"
    ).check  # a new process: the instance is new, the memory is not
    again.start(Memory(memory.data))
    again._now = lambda: T0 + 1800
    result = again.run(10)
    assert result.level == "warn" and "6 restarts in 1 h" in (result.summary or "")


def test_without_memory_the_history_starts_fresh(tmp_path: Path, docker: FakeDocker) -> None:
    """Past the ttl the scheduler hands over no data (continuity is broken): the first sighting is no growth."""
    first = build(tmp_path, docker).check
    memory = Memory()
    first.start(memory)
    first._now = lambda: T0
    first.run(10)
    docker.set_state("stack-demo-1", restarts=9)
    again = build(tmp_path, docker).check
    again.start(Memory(None))
    again._now = lambda: T0 + 600
    assert again.run(10).level == "ok"


def test_a_recreated_container_starts_its_history_again(tmp_path: Path, docker: FakeDocker) -> None:
    spec = build(tmp_path, docker, "restarts = 1")
    docker.set_state("stack-demo-1", restarts=10)
    run(spec, T0)
    docker.recreate("stack-demo-1", "ab" * 32)
    assert run(spec, T0 + 60)[1].level == "ok"  # the count dropped to 0: no growth, no negative nonsense
    docker.set_state("stack-demo-1", restarts=4)
    assert run(spec, T0 + 120)[1].level == "warn"


def test_a_corrupt_memory_is_ignored(tmp_path: Path, docker: FakeDocker) -> None:
    check = build(tmp_path, docker).check
    check.start(Memory({"restarts": {"stack-demo-1": {"id": "x", "samples": "nonsense"}, "bad": 3}}))
    assert check.run(10).level == "ok"


def test_a_long_list_is_clipped(tmp_path: Path, docker: FakeDocker) -> None:
    docker.add_container("extra", "x:1", "sha256:" + "1" * 64, None)
    for item in docker.list:
        docker.set_state(item["Names"][0].lstrip("/"), health="unhealthy", status="running")
    _, core = run(build(tmp_path, docker))
    assert core.level == "crit" and core.value == 9
    assert core.summary is not None and core.summary.endswith("; +3 more")
    assert len(core.summary or "") <= 200


# --- failures


def test_docker_unreachable_is_a_failure(tmp_path: Path) -> None:
    with FakeDocker() as docker:
        text = f'[[check]]\nid = "d"\ntype = "docker_status"\ndocker = "{docker.url}"\n'
    spec = parse(text, tmp_path).checks[0]  # the server is gone now
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == "Docker unreachable: connection refused"
    assert judge(spec, outcome).level == "unknown"


def test_a_refusing_proxy_is_a_failure_that_says_so(tmp_path: Path, docker: FakeDocker) -> None:
    docker.refuse = 403
    outcome = execute(build(tmp_path, docker))
    assert isinstance(outcome, Failure)
    assert outcome.summary == "Docker API refused the request (HTTP 403): is that path allowed?"


def test_an_unexpected_answer_is_a_failure(tmp_path: Path, docker: FakeDocker) -> None:
    docker.refuse = 500
    outcome = execute(build(tmp_path, docker))
    assert isinstance(outcome, Failure) and outcome.summary == "Docker API answered HTTP 500"


# --- config


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('containers = ["a"]\nlabels = ["b"]', "either 'containers' or 'labels'"),
        ("warn = 1", "decides its own level"),
        ("crit = 1", "decides its own level"),
        ('docker = "http://x"', "must be"),
        ('docker = "tcp://host:port"', "port is not a number"),
        ("restarts = -1", "'restarts' must be at least 0"),
        ('restart_window = "soon"', "restart_window"),
        ("containers = []", "'containers' is empty"),
        ("bogus = 1", "unknown key 'bogus'"),
    ],
)
def test_config_errors(tmp_path: Path, extra: str, message: str) -> None:
    text = f'[[check]]\nid = "d"\ntype = "docker_status"\n{extra}\n'
    if "docker =" not in extra:
        text += 'docker = "tcp://localhost:2375"\n'
    with pytest.raises(ConfigError) as err:
        parse(text, tmp_path)
    assert message in "; ".join(err.value.problems)


def test_unix_sockets_are_a_config_error_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("anystatus.checks.docker_api.is_windows", lambda: True)
    with pytest.raises(ConfigError) as err:
        parse('[[check]]\nid = "d"\ntype = "docker_status"\n', tmp_path)  # the default is the unix socket
    assert "not available on Windows" in err.value.problems[0] and "tcp://" in err.value.problems[0]


def test_endpoints() -> None:
    assert parse_endpoint("tcp://proxy:2375").port == 2375
    assert parse_endpoint("tcp://proxy").port == 2375
    if not is_windows():
        assert parse_endpoint("unix:///var/run/docker.sock").unix_path == "/var/run/docker.sock"
    with pytest.raises(ValueError):
        parse_endpoint("tcp://user:pw@proxy:2375")


# --- the client


def test_the_client_only_ever_sends_get(
    tmp_path: Path, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    original = http.client.HTTPConnection.request

    def spy(self: http.client.HTTPConnection, method: str, *args: Any, **kwargs: Any) -> None:
        sent.append(method)
        original(self, method, *args, **kwargs)

    monkeypatch.setattr(http.client.HTTPConnection, "request", spy)
    execute(build(tmp_path, docker))
    execute(build(tmp_path, docker, 'containers = ["stack-demo-1"]'))
    assert sent and set(sent) == {"GET"}
    assert docker.methods() == {"GET"}
    # and the client offers nothing else to call
    public = {name for name in dir(DockerClient) if not name.startswith("_")}
    assert public == {"get_json"}


def test_running_images_of_the_captured_fixture(docker: FakeDocker) -> None:
    images = running_images(DockerClient(parse_endpoint(docker.url)), 5)
    by_ref = {i.reference: i for i in images}
    assert set(by_ref) == {
        "python:3.14-alpine",
        "ghcr.io/astral-sh/uv:0.13.7-python3.14-alpine",
        "karsten13/magicmirror:alpine-v2.38.0",
        "eclipse-mosquitto:2.0.22",
    }
    assert sorted(by_ref["python:3.14-alpine"].containers) == [
        "stack-demo-1",
        "stack-tool-1",
    ]  # one image, two containers
    assert sorted(by_ref["eclipse-mosquitto:2.0.22"].containers) == [
        "stack-mosquitto-1",
        "stack-mqtt-publisher-1",
    ]


def test_the_check_is_registered_with_no_default_limits(tmp_path: Path, docker: FakeDocker) -> None:
    assert isinstance(build(tmp_path, docker).check, DockerStatus)


@pytest.mark.skipif(is_windows(), reason="unix sockets: the checker reaches Docker over tcp:// on Windows")
def test_a_unix_socket(tmp_path: Path) -> None:
    import http.server
    import socketserver
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"[]" if self.path.startswith("/containers/json") else b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    path = tmp_path / "d.sock"
    server = socketserver.UnixStreamServer(str(path), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        text = f'[[check]]\nid = "d"\ntype = "docker_status"\ndocker = "unix://{path}"\n'
        spec = parse(text, tmp_path).checks[0]
        outcome = execute(spec)
        assert outcome == Observation(level="ok", summary="no containers", value=0)
    finally:
        server.shutdown()
        server.server_close()
