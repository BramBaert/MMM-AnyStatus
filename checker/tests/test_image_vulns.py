from __future__ import annotations

import json
import os
import sys
import textwrap
import time
import urllib.request
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeDocker, FakeRegistry, digest, fixture, trivy_calls, trivy_json, write_fake_trivy
from helpers import START, FakeClock, parse

from anystatus.checks.image_vulns import Applied, ImageVulns, apply_ladder, parse_trivy, render_vulns_page
from anystatus.cli import pages
from anystatus.document import Snapshot
from anystatus.judge import judge
from anystatus.model import Failure, Observation
from anystatus.scheduler import StoreMemory, execute
from anystatus.server import ResultsServer
from anystatus.table import ConfigError

DAY = timedelta(days=1)
# The schedule of the README's example, in short: what each class and severity does, and when.
SCHEDULE = """
[check.schedule.update]
CRITICAL = { warn = "0d", crit = "3d" }
HIGH = { warn = "7d", crit = "never" }
[check.schedule.rebuild]
CRITICAL = { warn = "0d", crit = "7d" }
HIGH = { warn = "0d", crit = "never" }
[check.schedule.waiting]
HIGH = { warn = "0d", crit = "never" }
[check.schedule.no_fix]
CRITICAL = { warn = "never", crit = "0d" }
"""


@pytest.fixture
def docker() -> Iterator[FakeDocker]:
    with FakeDocker(empty=True) as fake:
        yield fake


@pytest.fixture
def reg() -> Iterator[FakeRegistry]:
    with FakeRegistry() as fake:
        yield fake


def containers(docker: FakeDocker, *images: str) -> None:
    """Containers of images Docker can't say anything about (no Identity, no RepoDigests, no layers)."""
    for index, image in enumerate(images):
        docker.add_container(f"c{index}", image, "sha256:" + str(index + 1) * 64, None)


def pulled(docker: FakeDocker, reg: FakeRegistry, repo: str, tag: str, build: str) -> str:
    """A running container of reg.host/repo:tag, pulled from the registry as `build`. Returns its reference."""
    name = f"{reg.host}/{repo}"
    image_id = "sha256:" + digest(f"{repo}:{tag}:{build}").split(":")[1]
    docker.add_image(image_id, [f"{name}:{tag}"], [f"{name}@{digest(build)}"], [digest(f"{repo}-layer")], {"Pull": [{"Repository": name}]})
    docker.add_container(repo.rsplit("/", 1)[-1], f"{name}:{tag}", image_id, None)
    return f"{name}:{tag}"


def build(
    tmp_path: Path,
    docker: FakeDocker,
    trivy: list[str],
    extra: str = "",
    timeout: str = "20s",
    env: list[str] | None = None,
) -> Any:
    text = (
        f'[[check]]\nid = "v"\ntype = "image_vulns"\nlabel = "Vulns"\ndocker = "{docker.url}"\ninterval = "1d"\ntimeout = "{timeout}"\n'
        f"trivy = {json.dumps(trivy)}\nenv = {json.dumps((['SYSTEMROOT'] if sys.platform == 'win32' else []) + (env or []))}\n{extra}\n"
    )
    spec = parse(text, tmp_path).checks[0]
    spec.check.now = lambda: START
    return spec


def at(spec: Any, moment: Any) -> Any:
    spec.check.now = lambda: moment
    return spec


def run(spec: Any) -> tuple[Any, Any]:
    outcome = execute(spec)
    return outcome, judge(spec, outcome)


def rows(spec: Any) -> list[tuple[str, str, str, str]]:
    """(image, CVE, class, level) of every row of the last report."""
    return [(f.image, f.vuln.id, f.klass, f.level) for f in spec.check.report.findings]


# --- reading Trivy


def test_parse_the_captured_trivy_output() -> None:
    vulns = parse_trivy(json.dumps(fixture("trivy_alpine_3_10.json")))
    assert [(v.id, v.package, v.severity, v.fixed) for v in vulns] == [("CVE-2021-36159", "apk-tools", "CRITICAL", "2.10.7-r0")]


def test_parse_a_trivy_0_76_report_with_layers_and_status() -> None:
    """Captured from Trivy 0.76.2 on an image built FROM python:3.14-slim (trimmed): every vulnerability, with or
    without a fix, and the layer (DiffID) its package came with."""
    vulns = {v.id: v for v in parse_trivy(json.dumps(fixture("trivy_python_slim.json")))}
    assert len(vulns) == 11
    zlib = vulns["CVE-2026-76844"]
    assert (zlib.package, zlib.severity, zlib.installed, zlib.fixed, zlib.status) == (
        "zlib1g",
        "HIGH",
        "1:1.3.dfsg+really1.3.1-1+b1",
        "1:1.3.dfsg+really1.3.1-1+deb13u1",
        "fixed",
    )
    assert zlib.layer == "sha256:dd57f90311a2567b55ff22eeacff1602fd04ad7566e315957731359eab173b90"
    assert zlib.url == "https://avd.aquasec.com/nvd/cve-2026-76844"
    assert (vulns["CVE-2026-9538"].fixed, vulns["CVE-2026-9538"].status) == ("", "fix_deferred")
    assert vulns["CVE-2026-114613"].package == "msgpack"  # a Python package, not only OS packages


def test_trivy_says_nothing_about_known_exploited_vulnerabilities() -> None:
    """Whether a CVE is on CISA's KEV list: Trivy 0.76's report has no field for it (checked on a real report; only
    free text such as descriptions mentions exploits)."""
    for result in fixture("trivy_python_slim.json")["Results"]:
        for vuln in result["Vulnerabilities"]:
            assert not any("kev" in key.lower() or "exploit" in key.lower() for key in vuln)


def test_results_without_vulnerabilities_and_odd_shapes() -> None:
    assert parse_trivy('{"Results": [{"Target": "x", "Vulnerabilities": null}, 5]}') == []
    assert parse_trivy('{"SchemaVersion": 2}') == []
    for bad in ("not json", "[]", '"a string"'):
        with pytest.raises(ValueError):
            parse_trivy(bad)


# --- the classes


def test_update_or_waiting_depends_on_the_newer_build(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    """A newer build of the tag exists: what it fixed is `update`, what it still has is `waiting`. Without --ignore-
    unfixed: what has no fix at all is `no_fix`."""
    db = pulled(docker, reg, "acme/db", "11", "db-1")
    reg.add("acme/db", "11", index=digest("db-2"))  # rebuilt upstream
    command = write_fake_trivy(
        tmp_path,
        {
            db: trivy_json([("CVE-2026-1001", "CRITICAL", "1.1"), ("CVE-2026-1002", "CRITICAL", "2.0"), ("CVE-2026-1003", "CRITICAL", None)]),
            f"remote:{db}": trivy_json([("CVE-2026-1002", "CRITICAL", "2.0"), ("CVE-2026-1003", "CRITICAL", None)]),
        },
    )
    spec = build(tmp_path, docker, command, SCHEDULE)
    _, core = run(spec)
    assert rows(spec) == [
        ("db", "CVE-2026-1003", "no_fix", "crit"),  # no fix exists: red at once (the user may have to act anyway)
        ("db", "CVE-2026-1001", "update", "warn"),
        ("db", "CVE-2026-1002", "waiting", "warn"),  # waiting lists only HIGH: CRITICAL follows HIGH's line
    ]
    assert (core.level, core.summary, core.value) == ("crit", "db: 1 critical, no fix exists", 3)
    local, remote = trivy_calls(tmp_path)
    assert "--ignore-unfixed" not in local and "--image-src" not in local
    assert remote[remote.index("--image-src") + 1] == "remote" and remote[-1] == db


def test_without_a_newer_build_a_fix_is_waiting_and_nothing_is_scanned_twice(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    db = pulled(docker, reg, "acme/db", "11", "db-1")
    reg.add("acme/db", "11", index=digest("db-1"))  # the same build
    command = write_fake_trivy(tmp_path, {db: trivy_json([("CVE-2026-2001", "HIGH", "1.1")])})
    spec = build(tmp_path, docker, command, SCHEDULE)
    _, core = run(spec)
    assert (core.level, core.summary) == ("warn", "db: 1 high, waiting for upstream fix")
    assert len(trivy_calls(tmp_path)) == 1


def test_a_locally_built_image_rebuild_or_waiting_by_layer(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    """The captured image built FROM python:3.14-slim and the captured Trivy report. Every vulnerability in it came
    with a base layer. The base has a newer build that fixes zlib's CVE-2026-76844 but nothing else: that one is
    `rebuild`, the other fixed ones `waiting`; one without a fix is `no_fix`."""
    captured = fixture("docker_local_build.json")
    built, base = captured["anystatus-localtest:dev"], captured["python:3.14-slim"]
    python = f"{reg.host}/library/python"
    docker.add_image(
        base["Id"], [f"{python}:3.14-slim"], [f"{python}@{digest('py-1')}"], base["RootFS"]["Layers"], {"Pull": [{"Repository": python}]}
    )
    docker.add_image(built["Id"], built["RepoTags"], built["RepoDigests"], built["RootFS"]["Layers"], built["Identity"])
    docker.add_container("app", "anystatus-localtest:dev", built["Id"], None)
    reg.add("library/python", "3.14-slim", index=digest("py-2"))
    report = fixture("trivy_python_slim.json")
    newer_base = json.loads(json.dumps(report))
    for result in newer_base["Results"]:
        result["Vulnerabilities"] = [v for v in result["Vulnerabilities"] if v["VulnerabilityID"] != "CVE-2026-76844"]
    command = write_fake_trivy(tmp_path, {"anystatus-localtest:dev": report, f"remote:{python}:3.14-slim": newer_base})
    spec = build(tmp_path, docker, command, SCHEDULE)
    _, core = run(spec)
    classes = {cve: klass for _, cve, klass, _ in rows(spec)}
    assert classes["CVE-2026-76844"] == "rebuild"
    assert classes["CVE-2026-27171"] == classes["CVE-2026-114613"] == "waiting"
    assert classes["CVE-2026-9538"] == classes["CVE-2026-76642"] == "no_fix"
    # both a rebuild HIGH and a waiting HIGH are amber; the card names what the user can do
    assert (core.level, core.summary) == ("warn", "anystatus-localtest: 1 high, fixed, please rebuild image")
    assert not any("anystatus-localtest" in p for _, p in reg.requests)


def test_a_fix_in_the_images_own_layers_is_rebuild(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    """A package the image's own Dockerfile installed: the fix is in the user's hands, whatever the base does."""
    layers = [digest("base-layer"), digest("own-layer")]
    python = f"{reg.host}/library/python"
    docker.add_image("sha256:" + "b" * 64, [f"{python}:3"], [f"{python}@{digest('py')}"], layers[:1], {"Pull": [{"Repository": python}]})
    docker.add_image("sha256:" + "a" * 64, ["mine:1"], [], layers, {"Build": [{}]})
    docker.add_container("mine", "mine:1", "sha256:" + "a" * 64, None)
    reg.add("library/python", "3", index=digest("py"))  # the base is current
    command = write_fake_trivy(tmp_path, {"mine:1": trivy_json([("CVE-2026-3001", "CRITICAL", "2.0")], layer=layers[1])})
    spec = build(tmp_path, docker, command, SCHEDULE)
    _, core = run(spec)
    assert rows(spec) == [("mine", "CVE-2026-3001", "rebuild", "warn")]
    assert core.summary == "mine: 1 critical, fixed, please rebuild image"
    assert len(trivy_calls(tmp_path)) == 1  # the base has no newer build: not scanned


def test_a_registry_that_does_not_answer_leaves_a_fix_waiting_and_says_so_on_the_page(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    db = pulled(docker, reg, "acme/db", "11", "db-1")
    reg.status = 503
    command = write_fake_trivy(tmp_path, {db: trivy_json([("CVE-2026-4001", "HIGH", "1.1")])})
    spec = build(tmp_path, docker, command, SCHEDULE)
    _, core = run(spec)
    assert core.summary == "db: 1 high, waiting for upstream fix"  # the card stays short
    assert spec.check.report.notes == ("newer build not checked: db",)


# --- the schedule


def test_each_cve_is_counted_from_when_it_was_first_found(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    db = pulled(docker, reg, "acme/db", "11", "db-1")
    reg.add("acme/db", "11", index=digest("db-2"))
    outputs: dict[str, Any] = {db: trivy_json([("CVE-2026-5001", "CRITICAL", "1.1")]), f"remote:{db}": trivy_json([])}
    command = write_fake_trivy(tmp_path, outputs)
    spec = build(tmp_path, docker, command, SCHEDULE)
    memory = StoreMemory("v", None, FakeClock(), None)
    spec.check.start(memory)
    assert run(at(spec, START))[1].level == "warn"  # update CRITICAL: amber at once...
    assert run(at(spec, START + 3 * DAY - timedelta(seconds=1)))[1].level == "warn"
    _, core = run(at(spec, START + 3 * DAY))  # ...red after 3 days
    assert (core.level, core.summary) == ("crit", "db: 1 critical, fixed, please update image")
    # a new critical doesn't inherit the age of the old one
    outputs[db] = trivy_json([("CVE-2026-5001", "CRITICAL", "1.1"), ("CVE-2026-5002", "CRITICAL", "1.1")])
    command = write_fake_trivy(tmp_path, outputs)
    _, core = run(at(spec, START + 4 * DAY))
    assert rows(spec) == [("db", "CVE-2026-5001", "update", "crit"), ("db", "CVE-2026-5002", "update", "warn")]
    assert core.value == 2
    # the dates are in the memory file's data, so a restart (within the ttl) keeps them
    assert memory.data == {"found": {f"{db}|CVE-2026-5001": "2026-11-11T12:00:00Z", f"{db}|CVE-2026-5002": "2026-11-15T12:00:00Z"}}
    restarted = build(tmp_path, docker, command, SCHEDULE)
    restarted.check.start(StoreMemory("v", None, FakeClock(), memory.data))
    assert run(at(restarted, START + 4 * DAY))[1].level == "crit"


def test_a_cve_that_goes_away_and_comes_back_is_new(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    db = pulled(docker, reg, "acme/db", "11", "db-1")
    reg.add("acme/db", "11", index=digest("db-2"))
    outputs: dict[str, Any] = {db: trivy_json([("CVE-2026-6001", "CRITICAL", "1.1")]), f"remote:{db}": trivy_json([])}
    spec = build(tmp_path, docker, write_fake_trivy(tmp_path, outputs), SCHEDULE)
    spec.check.start(StoreMemory("v", None, FakeClock(), None))
    run(at(spec, START))
    outputs[db] = trivy_json([])
    write_fake_trivy(tmp_path, outputs)
    run(at(spec, START + DAY))
    outputs[db] = trivy_json([("CVE-2026-6001", "CRITICAL", "1.1")])
    write_fake_trivy(tmp_path, outputs)
    run(at(spec, START + 5 * DAY))
    assert spec.check.report.findings[0].first_found == START + 5 * DAY


def test_a_higher_severity_follows_the_line_below_and_a_lower_one_is_ignored(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    db = pulled(docker, reg, "acme/db", "11", "db-1")
    reg.add("acme/db", "11", index=digest("db-1"))
    output = trivy_json([("CVE-2026-7001", "CRITICAL", "1.1"), ("CVE-2026-7002", "MEDIUM", "1.1")])
    spec = build(tmp_path, docker, write_fake_trivy(tmp_path, {db: output}), SCHEDULE)
    _, core = run(at(spec, START + 100 * DAY))
    # waiting lists only HIGH: a waiting CRITICAL follows HIGH's line (amber, never red); MEDIUM is below it: ignored
    assert (core.level, core.summary, core.value) == ("warn", "db: 1 critical, waiting for upstream fix", 1)
    assert [(f.vuln.severity, f.scheduled) for f in spec.check.report.findings] == [("CRITICAL", True), ("MEDIUM", False)]


# --- the ladder: a severity that isn't listed follows the nearest listed one below it


def by_severity(spec: Any) -> dict[str, str]:
    """severity -> level now, or "-" when its class and severity don't count."""
    return {f.vuln.severity: f.level if f.scheduled else "-" for f in spec.check.report.findings}


def waiting_only(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry, schedule: str) -> Any:
    """A registry image without a newer build (every fix is `waiting`), one fixed CVE of each severity."""
    db = pulled(docker, reg, "acme/db", "11", "db-1")
    reg.add("acme/db", "11", index=digest("db-1"))
    output = trivy_json([(f"CVE-2026-{i}000", severity, "2.0") for i, severity in enumerate(["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"], 1)])
    return build(tmp_path, docker, write_fake_trivy(tmp_path, {db: output}), schedule)


def test_a_class_with_only_medium_counts_high_and_critical_on_its_line(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    spec = waiting_only(tmp_path, docker, reg, '[check.schedule.waiting]\nMEDIUM = { warn = "0d", crit = "10d" }\n')
    run(at(spec, START))
    assert by_severity(spec) == {"CRITICAL": "warn", "HIGH": "warn", "MEDIUM": "warn", "LOW": "-", "UNKNOWN": "-"}
    _, core = run(at(spec, START + 10 * DAY))
    assert by_severity(spec) == {"CRITICAL": "crit", "HIGH": "crit", "MEDIUM": "crit", "LOW": "-", "UNKNOWN": "-"}
    assert (core.level, core.summary, core.value) == ("crit", "db: 1 critical, waiting for upstream fix", 3)


def test_the_card_names_every_image_worst_first(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    outputs = {}
    for name, severities in (("a", ["CRITICAL"]), ("b", ["CRITICAL"] * 3), ("c", ["HIGH"] * 2), ("d", ["HIGH"])):
        ref = pulled(docker, reg, f"acme/{name}", "1", f"{name}-1")
        reg.add(f"acme/{name}", "1", index=digest(f"{name}-1"))  # no newer build: every fix is waiting
        outputs[ref] = trivy_json([(f"CVE-2026-{i}", severity, "2.0") for i, severity in enumerate(severities, 1)])
    spec = build(tmp_path, docker, write_fake_trivy(tmp_path, outputs), SCHEDULE)
    _, core = run(spec)
    expected = "b: 3 critical, waiting for upstream fix\na: 1 critical, waiting for upstream fix\nc: 2 high, waiting for upstream fix\n+1 more image"
    assert (core.level, core.summary, core.value) == ("warn", expected, 7)


def test_a_listed_never_stays_ignored(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    schedule = '[check.schedule.waiting]\nMEDIUM = { warn = "0d", crit = "10d" }\nCRITICAL = { warn = "never", crit = "never" }\n'
    spec = waiting_only(tmp_path, docker, reg, schedule)
    run(at(spec, START))
    _, core = run(at(spec, START + 100 * DAY))
    assert by_severity(spec) == {"CRITICAL": "-", "HIGH": "crit", "MEDIUM": "crit", "LOW": "-", "UNKNOWN": "-"}
    assert core.summary == "db: 1 high, waiting for upstream fix"


def test_unknown_neither_inherits_nor_passes_on(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    spec = waiting_only(tmp_path, docker, reg, '[check.schedule.waiting]\nUNKNOWN = { warn = "0d", crit = "never" }\n')
    _, core = run(spec)
    assert by_severity(spec) == {"CRITICAL": "-", "HIGH": "-", "MEDIUM": "-", "LOW": "-", "UNKNOWN": "warn"}
    assert core.summary == "db: 1 unknown, waiting for upstream fix"
    # and with LOW listed (the bottom of the ladder), UNKNOWN still follows nothing
    assert apply_ladder({"LOW": (0.0, 1.0)}) == {s: Applied((0.0, 1.0), "LOW") for s in ("LOW", "MEDIUM", "HIGH", "CRITICAL")}


def test_a_class_with_nothing_listed_counts_nothing(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    schedule = '[check.schedule.waiting]\n[check.schedule.update]\nLOW = { warn = "0d", crit = "0d" }\n'
    spec = waiting_only(tmp_path, docker, reg, schedule)
    _, core = run(at(spec, START + 100 * DAY))
    assert set(by_severity(spec).values()) == {"-"}
    assert (core.level, core.value) == ("ok", 0)
    assert apply_ladder({}) == {}


def test_the_page_shows_the_schedule_as_it_applies(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    schedule = '[check.schedule.waiting]\nMEDIUM = { warn = "0d", crit = "10d" }\nCRITICAL = { warn = "never", crit = "never" }\n'
    spec = waiting_only(tmp_path, docker, reg, schedule)
    page = render_vulns_page("home", [spec]).decode("utf-8")  # before the first run too: it is the config
    waiting = next(row for row in page.split("<tr>") if row.startswith("<td>waiting</td>"))
    cells = [c.split("</td>")[0] for c in waiting.split("<td>")[2:]]
    assert cells == [
        "never (not counted)",
        "as MEDIUM: amber at once, red 10d",
        "amber at once, red 10d",
        f"{chr(0x2013)} not counted",
        f"{chr(0x2013)} not counted",
    ]
    assert "<th>critical</th><th>high</th><th>medium</th><th>low</th><th>unknown</th>" in page


def test_without_a_schedule_it_counts_fixed_critical_and_high(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    """The default: a fixed CRITICAL is red and a fixed HIGH amber at once, whatever the class; no fix: not counted."""
    containers(docker, "grafana/grafana:11.2", "nginx:1.27", "redis:7")
    command = write_fake_trivy(
        tmp_path,
        {
            "grafana/grafana:11.2": trivy_json(
                [("CVE-2024-1111", "CRITICAL", "11.3.1"), ("CVE-2024-2222", "CRITICAL", "11.3.1"), ("CVE-2024-3333", "HIGH", "9.9")]
            ),
            "nginx:1.27": trivy_json([("CVE-2024-4444", "HIGH", "1.27.4"), ("CVE-2024-5555", "LOW", "1.0")]),
            "redis:7": trivy_json([("CVE-2024-6666", "CRITICAL", None)]),
        },
    )
    _, core = run(build(tmp_path, docker, command))
    assert (core.level, core.value) == ("crit", 4)
    # Docker can't say where these came from (no Identity, no RepoDigests, no layers): built here, base unknown
    assert core.summary == "grafana: 2 critical, fixed, please rebuild image\nnginx: 1 high, fixed, please rebuild image"


def test_a_fixable_critical_from_the_captured_alpine_report(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "alpine:3.10")
    command = write_fake_trivy(tmp_path, {"alpine:3.10": fixture("trivy_alpine_3_10.json")})
    outcome, core = run(build(tmp_path, docker, command))
    assert isinstance(outcome, Observation)
    assert (core.level, core.value) == ("crit", 1)
    assert core.summary == "alpine: 1 critical, fixed, please rebuild image"
    # exactly the documented command line: a list, no shell, the image last
    (call,) = trivy_calls(tmp_path)
    assert call[:6] == ["image", "--format", "json", "--quiet", "--scanners", "vuln"]
    assert call[-1] == "alpine:3.10" and "--cache-dir" not in call


def test_nothing_due(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "a:1", "b:1")
    command = write_fake_trivy(tmp_path, {}, default=trivy_json([("CVE-2024-1", "CRITICAL", None)]))
    _, core = run(build(tmp_path, docker, command))
    assert (core.level, core.value, core.summary) == ("ok", 0, "nothing due in 2 images")


# --- the page


def test_the_page_lists_every_vulnerability(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    db = pulled(docker, reg, "acme/db", "11", "db-1")
    reg.add("acme/db", "11", index=digest("db-1"))
    output = trivy_json([("CVE-2026-8001", "HIGH", "1.1"), ("CVE-2026-8002", "LOW", None)])
    output["Results"][0]["Vulnerabilities"][0]["PkgName"] = "<script>"
    output["Results"][0]["Vulnerabilities"][1]["Status"] = "will_not_fix"
    spec = build(tmp_path, docker, write_fake_trivy(tmp_path, {db: output}), SCHEDULE)
    assert b"No run yet." in render_vulns_page("home", [spec])
    run(spec)
    page = render_vulns_page("home", [spec]).decode("utf-8")
    for text in ("<h1>home: vulnerabilities</h1>", "Vulns", db, "CVE-2026-8001", "1.0", "1.1", "waiting", "2026-11-11T12:00:00Z"):
        assert text in page
    assert "no fix (will not fix)" in page
    assert "&lt;script&gt;" in page and "<script>" not in page
    assert page.index("CVE-2026-8001") < page.index("CVE-2026-8002")  # what is due comes first


def test_the_page_is_served_next_to_results(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    db = pulled(docker, reg, "acme/db", "11", "db-1")
    reg.add("acme/db", "11", index=digest("db-1"))
    text = (
        f'[[check]]\nid = "v"\ntype = "image_vulns"\ndocker = "{docker.url}"\ninterval = "1d"\ntimeout = "20s"\n'
        f"trivy = {json.dumps(write_fake_trivy(tmp_path, {db: trivy_json([('CVE-2026-9001', 'HIGH', '2')])}))}\n"
        f"env = {json.dumps(['SYSTEMROOT'] if sys.platform == 'win32' else [])}\n"
    )
    config = parse(text, tmp_path)
    execute(config.checks[0])
    server = ResultsServer("127.0.0.1", 0, Snapshot("t"), pages(config))
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        with urllib.request.urlopen(f"{base}/vulns", timeout=5) as answer:
            assert answer.headers["Content-Type"] == "text/html; charset=utf-8"
            assert b"CVE-2026-9001" in answer.read()
        with urllib.request.urlopen(f"{base}/", timeout=5) as answer:
            assert b"GET /vulns for the full list of vulnerabilities" in answer.read()
    finally:
        server.stop()
    assert pages(parse('[[check]]\nid = "a"\ntype = "scripted"\n', tmp_path)) == {}  # no image_vulns: no page


# --- what stayed: scanning, environment, failures


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


def test_trivy_gets_only_the_listed_environment(tmp_path: Path, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch) -> None:
    containers(docker, "a:1")
    monkeypatch.setenv("SOME_SECRET", "hunter2")
    monkeypatch.setenv("LISTED", "yes")
    monkeypatch.setenv("DOCKER_HOST", "tcp://the-checkers-own:2375")
    script = tmp_path / "env_trivy.py"
    script.write_text(
        "import json, os, sys\nprint(json.dumps({'Results': []}))\n"
        f"open({str(tmp_path / 'env.txt')!r}, 'w').write(repr(sorted((k, v) for k, v in os.environ.items() if k in ('SOME_SECRET', 'LISTED', 'DOCKER_HOST'))))\n",
        encoding="utf-8",
    )
    spec = build(tmp_path, docker, [sys.executable, str(script)], env=["LISTED"])
    run(spec)
    # DOCKER_HOST is the check's own 'docker' address, not the checker's environment
    assert (tmp_path / "env.txt").read_text() == repr([("DOCKER_HOST", docker.url), ("LISTED", "yes")])


def test_docker_host_in_env_is_refused(tmp_path: Path, docker: FakeDocker) -> None:
    with pytest.raises(ConfigError, match="DOCKER_HOST comes from the check's 'docker' key"):
        build(tmp_path, docker, [sys.executable, "-c", "pass"], env=["DOCKER_HOST"])


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
    assert spec.check.report.error == "trivy not found"


def test_a_failing_scan_leaves_that_image_unscanned_and_keeps_its_dates(tmp_path: Path, docker: FakeDocker) -> None:
    containers(docker, "a:1", "b:1")
    outputs: dict[str, Any] = {"a:1": trivy_json([("CVE-2024-9", "HIGH", "3")]), "b:1": trivy_json([("CVE-2024-1", "HIGH", "2")])}
    spec = build(tmp_path, docker, write_fake_trivy(tmp_path, outputs))
    spec.check.start(StoreMemory("v", None, FakeClock(), None))
    run(at(spec, START))
    outputs["a:1"] = "fail"
    write_fake_trivy(tmp_path, outputs)
    _, core = run(at(spec, START + DAY))
    assert (core.level, core.value) == ("warn", 1)
    assert core.summary == "b: 1 high, fixed, please rebuild image\n1 not scanned (trivy failed)"
    assert "a:1|CVE-2024-9" in spec.check.found  # not seen this time, but not gone either


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
    command = write_fake_trivy(tmp_path, {"a:1": trivy_json([("CVE-2024-1", "CRITICAL", "2")]), "b:1": "hang"})
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
        ('severities = ["HIGH"]', "'severities' has been replaced by 'schedule'"),
        ('[check.schedule.later]\nHIGH = { warn = "0d", crit = "never" }', "unknown class 'later'"),
        ('[check.schedule.update]\nSEVERE = { warn = "0d", crit = "1d" }', "'schedule.update.SEVERE': unknown severity"),
        ('[check.schedule.update]\nHIGH = { warn = "0d" }', "must give both warn and crit"),
        ('[check.schedule.update]\nHIGH = { warn = "1w", crit = "never" }', "'schedule.update.HIGH': \"1w\" is not a time"),
        ('[check.schedule."no fix"]\nHIGH = { warn = "0d", crit = "never" }', "unknown class 'no fix'"),
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


def test_the_readme_example_is_valid(tmp_path: Path) -> None:
    """The README's Docker example, as written (only Trivy's path is this machine's)."""
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    block = next(b for b in readme.split("```toml")[1:] if 'id = "docker.vulns"' in b).split("```")[0]
    text = textwrap.dedent(block).replace('trivy = "/usr/local/bin/trivy"', f"trivy = {json.dumps([sys.executable])}")
    config = parse(text, tmp_path, env={"GHCR_AUTH": "u:p"})
    check = next(s.check for s in config.checks if s.id == "docker.vulns")
    assert isinstance(check, ImageVulns)
    assert check.listed["update"] == {"CRITICAL": (0.0, 3 * 86400.0), "HIGH": (7 * 86400.0, 30 * 86400.0), "MEDIUM": (30 * 86400.0, float("inf"))}
    # what the example's comments say is left out really doesn't count: every left-out severity is below the lowest
    # listed one, and UNKNOWN is listed nowhere
    assert set(check.schedule["update"]) == set(check.schedule["rebuild"]) == {"CRITICAL", "HIGH", "MEDIUM"}
    assert set(check.schedule["waiting"]) == {"CRITICAL", "HIGH"}
    assert check.schedule["no_fix"] == {"CRITICAL": Applied((float("inf"), 0.0), "CRITICAL")}
    assert all(line.source == severity for lines in check.schedule.values() for severity, line in lines.items())  # nothing inherited
    assert config.warnings == ()


def test_the_default_timeout_is_warned_about(tmp_path: Path) -> None:
    text = f'[[check]]\nid = "v"\ntype = "image_vulns"\ndocker = "tcp://localhost:2375"\ntrivy = {json.dumps([sys.executable])}\n'
    config = parse(text, tmp_path)
    assert any("too short for Trivy" in w for w in config.warnings)
    assert os.path.exists(sys.executable)
