from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeDocker, fixture, trivy_calls, trivy_json, write_fake_trivy
from helpers import parse

from anystatus.checks.image_vulns import parse_trivy
from anystatus.judge import judge
from anystatus.model import Failure, Observation
from anystatus.scheduler import execute
from anystatus.table import ConfigError


@pytest.fixture
def docker() -> Iterator[FakeDocker]:
    with FakeDocker(empty=True) as fake:
        yield fake


def containers(docker: FakeDocker, *images: str) -> None:
    for index, image in enumerate(images):
        docker.add_container(f"c{index}", image, "sha256:" + str(index + 1) * 64, None)


def build(
    tmp_path: Path,
    docker: FakeDocker,
    trivy: list[str],
    extra: str = "",
    timeout: str = "20s",
    env: list[str] | None = None,
) -> Any:
    text = (
        f'[[check]]\nid = "v"\ntype = "image_vulns"\ndocker = "{docker.url}"\ninterval = "1d"\ntimeout = "{timeout}"\n'
        f"trivy = {json.dumps(trivy)}\nenv = {json.dumps((['SYSTEMROOT'] if sys.platform == 'win32' else []) + (env or []))}\n{extra}\n"
    )
    return parse(text, tmp_path).checks[0]


def run(spec: Any) -> tuple[Any, Any]:
    outcome = execute(spec)
    return outcome, judge(spec, outcome)


def test_parse_the_captured_trivy_output() -> None:
    vulns = parse_trivy(json.dumps(fixture("trivy_alpine_3_10.json")))
    assert [(v.id, v.package, v.severity, v.fixed) for v in vulns] == [
        ("CVE-2021-36159", "apk-tools", "CRITICAL", "2.10.7-r0")
    ]


def test_only_fixable_vulnerabilities_are_parsed() -> None:
    raw = trivy_json(
        [
            ("CVE-2024-0001", "CRITICAL", "1.2.3"),
            ("CVE-2024-0002", "CRITICAL", None),
            ("CVE-2024-0003", "HIGH", ""),
        ]
    )
    assert [v.id for v in parse_trivy(json.dumps(raw))] == ["CVE-2024-0001"]


def test_results_without_vulnerabilities_and_odd_shapes() -> None:
    assert parse_trivy('{"Results": [{"Target": "x", "Vulnerabilities": null}, 5]}') == []
    assert parse_trivy('{"SchemaVersion": 2}') == []
    for bad in ("not json", "[]", '"a string"'):
        with pytest.raises(ValueError):
            parse_trivy(bad)


def test_a_fixable_critical_is_crit(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "alpine:3.10")
    command = write_fake_trivy(tmp_path, {"alpine:3.10": fixture("trivy_alpine_3_10.json")})
    outcome, core = run(build(tmp_path, docker, command))
    assert isinstance(outcome, Observation)
    assert (core.level, core.value) == ("crit", 1)
    assert core.summary == "alpine: 1 critical, fixed in 2.10.7-r0"
    # exactly the documented command line: a list, no shell, the image last
    (call,) = trivy_calls(tmp_path)
    assert call[:6] == ["image", "--format", "json", "--ignore-unfixed", "--quiet", "--scanners"]
    assert call[-1] == "alpine:3.10" and "--cache-dir" not in call


def test_high_is_warn_and_the_worst_image_is_named(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "grafana/grafana:11.2", "nginx:1.27", "redis:7")
    command = write_fake_trivy(
        tmp_path,
        {
            "grafana/grafana:11.2": trivy_json(
                [
                    ("CVE-2024-1111", "CRITICAL", "11.3.1"),
                    ("CVE-2024-2222", "CRITICAL", "11.3.1"),
                    ("CVE-2024-3333", "HIGH", "9.9"),
                ]
            ),
            "nginx:1.27": trivy_json([("CVE-2024-4444", "HIGH", "1.27.4"), ("CVE-2024-5555", "LOW", "1.0")]),
            "redis:7": trivy_json([("CVE-2024-6666", "HIGH", None)]),  # no fix: doesn't count
        },
    )
    _, core = run(build(tmp_path, docker, command))
    assert (core.level, core.value) == ("crit", 4)  # HIGH and CRITICAL count by default, LOW does not
    assert core.summary == "grafana: 2 critical, 1 high, fixed in 11.3.1; 1 more image affected"


def test_high_only_is_warn(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "nginx:1.27")
    command = write_fake_trivy(
        tmp_path,
        {
            "nginx:1.27": trivy_json(
                [("CVE-2024-4444", "HIGH", "1.27.4"), ("CVE-2024-4445", "HIGH", "1.27.5")]
            )
        },
    )
    _, core = run(build(tmp_path, docker, command))
    assert (core.level, core.value, core.summary) == ("warn", 2, "nginx: 2 high")  # two fixes: none is named


def test_severities_change_what_counts(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "nginx:1.27")
    command = write_fake_trivy(
        tmp_path,
        {
            "nginx:1.27": trivy_json(
                [("CVE-2024-1", "CRITICAL", "2"), ("CVE-2024-2", "HIGH", "3"), ("CVE-2024-3", "MEDIUM", "4")]
            )
        },
    )
    _, core = run(build(tmp_path, docker, command, 'severities = ["critical"]'))
    assert (core.level, core.value, core.summary) == ("crit", 1, "nginx: 1 critical, fixed in 2")
    _, core = run(build(tmp_path, docker, command, 'severities = ["HIGH", "MEDIUM"]'))
    assert (core.level, core.value, core.summary) == ("warn", 2, "nginx: 1 high, 1 medium, fixed in 3")


def test_nothing_fixable_is_fine(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "a:1", "b:1")
    command = write_fake_trivy(tmp_path, {}, default=trivy_json([("CVE-2024-1", "CRITICAL", None)]))
    _, core = run(build(tmp_path, docker, command))
    assert (core.level, core.value, core.summary) == ("ok", 0, "no fixable vulnerabilities in 2 images")


def test_each_image_is_scanned_once(tmp_path: Path, docker: FakeDocker) -> None:
    docker.add_container("a", "nginx:1.27", "sha256:" + "1" * 64, None)
    docker.add_container("b", "nginx:1.27", "sha256:" + "1" * 64, None)
    command = write_fake_trivy(tmp_path, {}, default=trivy_json([]))
    run(build(tmp_path, docker, command))
    assert len(trivy_calls(tmp_path)) == 1


def test_an_image_known_only_by_id_is_scanned_by_its_tag(tmp_path: Path, docker: FakeDocker) -> None:
    docker.add_container("a", "x", "sha256:" + "5" * 64, None, repo_tags=["app:2"])
    docker.list[0]["Image"] = docker.list[0]["ImageID"]
    command = write_fake_trivy(tmp_path, {}, default=trivy_json([]))
    run(build(tmp_path, docker, command))
    assert trivy_calls(tmp_path)[0][-1] == "app:2"


def test_cache_dir_is_passed(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "a:1")
    command = write_fake_trivy(tmp_path, {}, default=trivy_json([]))
    run(build(tmp_path, docker, command, 'cache_dir = "/var/cache/trivy"'))
    call = trivy_calls(tmp_path)[0]
    assert call[call.index("--cache-dir") + 1] == "/var/cache/trivy"


def test_trivy_gets_only_the_listed_environment(
    tmp_path: Path, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    containers(docker, "a:1")
    monkeypatch.setenv("SOME_SECRET", "hunter2")
    monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
    script = tmp_path / "env_trivy.py"
    script.write_text(
        "import json, os, sys\nprint(json.dumps({'Results': [], 'Env': sorted(k for k in os.environ if k in ('SOME_SECRET', 'DOCKER_HOST'))}))\n"
        f"open({str(tmp_path / 'env.txt')!r}, 'w').write(','.join(sorted(k for k in os.environ if k in ('SOME_SECRET', 'DOCKER_HOST'))))\n",
        encoding="utf-8",
    )
    spec = build(tmp_path, docker, [sys.executable, str(script)], env=["DOCKER_HOST"])
    run(spec)
    assert (tmp_path / "env.txt").read_text() == "DOCKER_HOST"


# --- failures


def test_trivy_not_found_is_a_config_error(tmp_path: Path) -> None:
    text = '[[check]]\nid = "v"\ntype = "image_vulns"\ntrivy = "no-such-trivy-anywhere"\ndocker = "tcp://localhost:2375"\n'
    with pytest.raises(ConfigError) as err:
        parse(text, tmp_path)
    assert "'trivy': 'no-such-trivy-anywhere' was not found" in err.value.problems[0]


def test_trivy_that_disappears_later_is_a_failure(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "a:1")
    fake = tmp_path / ("trivy.cmd" if sys.platform == "win32" else "trivy")
    fake.write_text("@echo {}\n" if sys.platform == "win32" else "#!/bin/sh\necho '{}'\n", encoding="utf-8")
    if sys.platform != "win32":
        fake.chmod(0o755)
    spec = build(tmp_path, docker, [str(fake)])
    fake.unlink()
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == "trivy not found"
    assert judge(spec, outcome).level == "unknown"


def test_a_failing_scan_leaves_that_image_unscanned(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "a:1", "b:1")
    command = write_fake_trivy(tmp_path, {"a:1": "fail", "b:1": trivy_json([("CVE-2024-1", "HIGH", "2")])})
    _, core = run(build(tmp_path, docker, command))
    assert (core.level, core.value) == ("warn", 1)
    assert core.summary == "b: 1 high, fixed in 2; 1 not scanned (trivy failed)"


def test_every_scan_failing_is_a_failure_without_trivys_stderr(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "a:1")
    command = write_fake_trivy(tmp_path, {"a:1": "fail"})
    spec = build(tmp_path, docker, command)
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == "trivy exit code 1"
    assert "FATAL" not in outcome.summary  # stderr goes to the (masked) debug log only


def test_bad_output_is_a_failure(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "a:1")
    command = write_fake_trivy(tmp_path, {"a:1": "not json at all"})
    outcome = execute(build(tmp_path, docker, command))
    assert isinstance(outcome, Failure)


def test_a_timeout_kills_trivy(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "a:1", "b:1")
    command = write_fake_trivy(
        tmp_path, {"a:1": trivy_json([("CVE-2024-1", "CRITICAL", "2")]), "b:1": "hang"}
    )
    spec = build(tmp_path, docker, command)
    started = time.monotonic()
    outcome = execute(parse_with_timeout(spec, 1.5))
    assert time.monotonic() - started < 20  # nowhere near the fake's 60 s sleep: the process was killed
    assert isinstance(outcome, Observation)
    assert outcome.level == "crit" and outcome.summary is not None
    assert outcome.summary.endswith("1 not scanned (timed out)")


def test_a_timeout_before_any_scan_is_a_failure(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "a:1")
    command = write_fake_trivy(tmp_path, {"a:1": "hang"})
    outcome = execute(parse_with_timeout(build(tmp_path, docker, command), 1.0))
    assert isinstance(outcome, Failure) and outcome.summary.startswith("timed out after")


def parse_with_timeout(spec: Any, seconds: float) -> Any:
    from dataclasses import replace

    return replace(spec, timeout=seconds)


def test_docker_unreachable_is_a_failure(tmp_path: Path) -> None:
    with FakeDocker(empty=True) as docker:
        spec = build(tmp_path, docker, [sys.executable, "-c", "pass"])
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == "Docker unreachable: connection refused"


def test_no_containers(tmp_path: Path, docker: FakeDocker) -> None:
    _, core = run(build(tmp_path, docker, [sys.executable, "-c", "pass"]))
    assert (core.level, core.summary) == ("ok", "no running containers")


# --- config


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("warn = 1", "decides its own level"),
        ("crit = 1", "decides its own level"),
        ('severities = ["SEVERE"]', "'severities' must be a list of"),
        ("trivy = 5", "'trivy' must be a command"),
        ("bogus = 1", "unknown key 'bogus'"),
    ],
)
def test_config_errors(tmp_path: Path, extra: str, message: str) -> None:
    text = f'[[check]]\nid = "v"\ntype = "image_vulns"\ndocker = "tcp://localhost:2375"\ntrivy = {json.dumps([sys.executable])}\n{extra}\n'
    if extra.startswith("trivy"):
        text = text.replace(f"trivy = {json.dumps([sys.executable])}\n", "")
    with pytest.raises(ConfigError) as err:
        parse(text, tmp_path)
    assert message in "; ".join(err.value.problems)


def test_the_default_timeout_is_warned_about(tmp_path: Path) -> None:
    text = f'[[check]]\nid = "v"\ntype = "image_vulns"\ndocker = "tcp://localhost:2375"\ntrivy = {json.dumps([sys.executable])}\n'
    config = parse(text, tmp_path)
    assert any("too short for Trivy" in w for w in config.warnings)
    assert os.path.exists(sys.executable)
