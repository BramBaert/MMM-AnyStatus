from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fakes import FakeDocker
from helpers import parse

from anystatus.checks.docker_api import DockerClient, is_windows, parse_endpoint, running_images
from anystatus.checks.docker_status import DockerStatus, health_of
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


def test_health_from_the_status_text_of_an_older_engine(tmp_path: Path, docker: FakeDocker) -> None:
    """Before Docker 29 the list has no Health object; its status text says it ("Up 3 hours (unhealthy)")."""
    docker.set_status("stack-demo-1", "Up 3 hours (unhealthy)")
    docker.set_status("stack-checker-1", "Up 5 seconds (health: starting)")
    _, core = run(build(tmp_path, docker))  # the fixture's magicmirror says "(healthy)"
    assert (core.level, core.value, core.summary) == ("crit", 1, "stack-demo-1: unhealthy")


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"Health": {"Status": "unhealthy"}, "Status": "Up 1 hour (healthy)"}, "unhealthy"),  # the object wins
        ({"Health": {"Status": "none"}, "Status": "Up 1 hour"}, "none"),
        ({"Status": "Up 1 hour (healthy)"}, "healthy"),
        ({"Status": "Up 2 seconds (health: starting)"}, "starting"),
        ({"Status": "Up 1 hour"}, None),
        ({}, None),
    ],
)
def test_health_of(item: dict[str, Any], expected: str | None) -> None:
    assert health_of(item) == expected


def test_default_ignores_stopped_containers_but_a_paused_listed_one_is_crit(tmp_path: Path, docker: FakeDocker) -> None:
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


def test_no_matching_container_is_fine(tmp_path: Path, docker: FakeDocker) -> None:
    _, core = run(build(tmp_path, docker, 'labels = ["nothing=here"]'))
    assert (core.level, core.value, core.summary) == ("ok", 0, "no containers")


# --- restarts: a container's `die` events within the window, kept in the check's memory


def events_asked(docker: FakeDocker) -> list[dict[str, list[str]]]:
    return [parse_qs(urlsplit(p).query) for _, p in docker.requests if urlsplit(p).path == "/events"]


def test_restarts_are_die_events_within_the_window(tmp_path: Path, docker: FakeDocker) -> None:
    spec = build(tmp_path, docker, "restarts = 3")
    for minutes in (50, 40, 30):
        docker.die("stack-demo-1", T0 - minutes * 60)
    assert run(spec, T0)[1].level == "ok"  # 3: not more than 3
    docker.die("stack-demo-1", T0 + 30)
    docker.die("stack-demo-1", T0 + 40)
    core = run(spec, T0 + 60)[1]
    assert (core.level, core.value, core.summary) == ("warn", 1, "stack-demo-1: 5 restarts in 1 h")


def test_only_die_events_of_containers_are_asked_for(tmp_path: Path, docker: FakeDocker) -> None:
    run(build(tmp_path, docker), T0)
    (asked,) = events_asked(docker)
    assert json.loads(asked["filters"][0]) == {"type": ["container"], "event": ["die"]}
    assert int(asked["until"][0]) == int(T0)


def test_each_run_asks_only_since_the_previous_one(tmp_path: Path, docker: FakeDocker) -> None:
    """A short stretch each time, well within the few hundred recent events the daemon keeps."""
    spec = build(tmp_path, docker, 'restart_window = "1h"')
    run(spec, T0)
    run(spec, T0 + 60)
    first, second = events_asked(docker)
    assert int(first["since"][0]) == int(T0 - 3600)  # nothing known yet: the whole window
    assert int(second["since"][0]) == int(T0)  # then from where the previous question ended


def test_an_event_in_the_boundary_second_is_counted_once(tmp_path: Path, docker: FakeDocker) -> None:
    spec = build(tmp_path, docker, "restarts = 1")
    docker.die("stack-demo-1", T0 - 100)
    docker.die("stack-demo-1", T0)  # in the second where the first question ends and the second one starts
    assert run(spec, T0)[1].level == "warn"  # 2 > 1
    core = run(spec, T0 + 60)[1]
    assert core.summary == "stack-demo-1: 2 restarts in 1 h"  # not 3


def test_the_window_moves_on(tmp_path: Path, docker: FakeDocker) -> None:
    spec = build(tmp_path, docker, 'restarts = 3\nrestart_window = "30m"')
    run(spec, T0)
    for seconds in range(10, 60, 10):
        docker.die("stack-demo-1", T0 + seconds)
    core = run(spec, T0 + 300)[1]
    assert core.level == "warn" and core.summary == "stack-demo-1: 5 restarts in 30 min"
    assert run(spec, T0 + 1500)[1].level == "warn"  # still inside the window
    assert run(spec, T0 + 3000)[1].level == "ok"  # the restarts are older than 30 min now


def test_old_events_outside_the_window_are_not_counted(tmp_path: Path, docker: FakeDocker) -> None:
    for minutes in (90, 80, 70, 65):
        docker.die("stack-demo-1", T0 - minutes * 60)
    assert run(build(tmp_path, docker, "restarts = 1"), T0)[1].level == "ok"


def test_crit_wins_over_warn_and_both_are_named(tmp_path: Path, docker: FakeDocker) -> None:
    spec = build(tmp_path, docker, "restarts = 1")
    run(spec, T0)
    for seconds in (10, 20, 30, 40, 50):
        docker.die("stack-demo-1", T0 + seconds)
    docker.set_state("stack-mosquitto-1", health="unhealthy")
    core = run(spec, T0 + 60)[1]
    assert core.level == "crit" and core.value == 2
    assert core.summary == "stack-mosquitto-1: unhealthy; stack-demo-1: 5 restarts in 1 h"


def test_the_history_survives_a_restart_of_the_checker(tmp_path: Path, docker: FakeDocker) -> None:
    memory = Memory()
    first = build(tmp_path, docker, "restarts = 3").check
    first.start(memory)
    first._now = lambda: T0
    docker.die("stack-demo-1", T0 - 60)
    docker.die("stack-demo-1", T0 - 30)
    first.run(10)
    assert memory.data is not None  # saved after the run

    docker.events.clear()  # the daemon's backlog no longer has them: only the memory does
    docker.die("stack-demo-1", T0 + 600)
    docker.die("stack-demo-1", T0 + 900)
    again = build(tmp_path, docker, "restarts = 3").check  # a new process: the instance is new, the memory is not
    again.start(Memory(memory.data))
    again._now = lambda: T0 + 1800
    result = again.run(10)
    assert result.level == "warn" and "4 restarts in 1 h" in (result.summary or "")


def test_without_memory_the_whole_window_is_asked_for(tmp_path: Path, docker: FakeDocker) -> None:
    """Past the ttl the scheduler hands over no data (continuity is broken): ask for the whole window, as far back as
    the daemon still has events."""
    for minutes in (50, 40, 30, 20, 10):
        docker.die("stack-demo-1", T0 - minutes * 60)
    check = build(tmp_path, docker, "restarts = 3").check
    check.start(Memory(None))
    check._now = lambda: T0
    assert check.run(10).level == "warn"


@pytest.mark.parametrize(
    "data",
    [
        {"restarts": {"stack-demo-1": {"id": "x", "samples": [[1, 2]]}}},  # the form before events
        {"dies": "nonsense", "until": 1, "newest": 1},
        {"dies": {"a": ["x"]}, "until": 1, "newest": 1},
        {"dies": {}, "until": "soon", "newest": 1},
    ],
)
def test_a_corrupt_or_old_memory_is_ignored(tmp_path: Path, docker: FakeDocker, data: dict[str, Any]) -> None:
    check = build(tmp_path, docker).check
    check.start(Memory(data))
    check._now = lambda: T0
    assert check.run(10).level == "ok"
    assert int(events_asked(docker)[0]["since"][0]) == int(T0 - 3600)  # as without a memory


def test_odd_events_are_skipped(tmp_path: Path, docker: FakeDocker) -> None:
    docker.events += [
        {"Type": "container", "Action": "die", "time": int(T0), "timeNano": "late"},
        {"Type": "container", "Action": "die", "time": int(T0), "timeNano": int(T0 * 1e9), "Actor": {}},
        {"Type": "container", "Action": "die", "time": int(T0), "timeNano": int(T0 * 1e9), "Actor": {"Attributes": {}}},
    ]
    assert run(build(tmp_path, docker, "restarts = 0"), T0)[1].level == "ok"


# --- what the check reads: never a container's details (they hold its environment, so its passwords)


def test_never_asks_for_a_containers_details(tmp_path: Path, docker: FakeDocker) -> None:
    docker.die("stack-demo-1", T0 - 60)
    docker.set_state("stack-mosquitto-1", health="unhealthy")
    for extra in ("", 'containers = ["stack-demo-1", "db-old", "nope"]', 'labels = ["x=y"]'):
        run(build(tmp_path, docker, extra), T0)
    paths = {urlsplit(p).path for _, p in docker.requests}
    assert paths == {"/containers/json", "/events"}


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


def test_a_proxy_that_refuses_events_says_which_path(tmp_path: Path, docker: FakeDocker) -> None:
    """The events are the restarts: a proxy that doesn't allow /events is a failure, not a silent 'no restarts'."""
    docker.refuse_paths["/events"] = 403
    outcome = execute(build(tmp_path, docker))
    assert isinstance(outcome, Failure)
    assert outcome.summary == "Docker API refused the request (HTTP 403): is that path allowed?"


def test_an_event_stream_that_isnt_json_is_a_failure(tmp_path: Path, docker: FakeDocker) -> None:
    docker.events_body = '{"Type": "container", "Action": "die"}\n{"Type": "contai'  # cut off halfway
    outcome = execute(build(tmp_path, docker))
    assert isinstance(outcome, Failure) and outcome.summary == "the Docker API answer is not JSON"


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


def test_the_client_only_ever_sends_get(tmp_path: Path, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert public == {"get_json", "get_json_stream"}


def test_a_stream_of_json_values(docker: FakeDocker) -> None:
    client = DockerClient(parse_endpoint(docker.url))
    docker.die("stack-demo-1", T0)
    docker.die("stack-checker-1", T0 + 1)
    query = {"since": str(int(T0)), "until": str(int(T0) + 1)}
    values = client.get_json_stream("/events", query, timeout=5)
    assert [v["Actor"]["Attributes"]["name"] for v in values] == ["stack-demo-1", "stack-checker-1"]
    assert client.get_json_stream("/events", {"since": "0", "until": "1"}, timeout=5) == []  # an empty stream


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
