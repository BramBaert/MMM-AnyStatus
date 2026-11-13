from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeDocker, FakeRegistry, digest, fixture
from helpers import parse

from anystatus.checks import registry as registry_api
from anystatus.checks.docker_updates import newest_version, parse_version_tag
from anystatus.checks.registry import ImageRef, parse_image
from anystatus.judge import judge
from anystatus.model import Failure, Reading
from anystatus.scheduler import execute
from anystatus.table import ConfigError


@pytest.fixture
def docker() -> Iterator[FakeDocker]:
    with FakeDocker(empty=True) as fake:
        yield fake


@pytest.fixture
def reg() -> Iterator[FakeRegistry]:
    with FakeRegistry() as fake:
        yield fake


def add(docker: FakeDocker, reg: FakeRegistry, name: str, repo: str, tag: str, local: str) -> None:
    """A running container of reg.host/repo:tag whose local image has this RepoDigest."""
    docker.add_container(
        name,
        f"{reg.host}/{repo}:{tag}",
        "sha256:" + digest(name).split(":")[1],
        [f"{reg.host}/{repo}@{local}"],
    )


def build(tmp_path: Path, docker: FakeDocker, extra: str = "", env: dict[str, str] | None = None) -> Any:
    text = f'[[check]]\nid = "u"\ntype = "docker_updates"\ndocker = "{docker.url}"\ninterval = "1d"\n{extra}\n'
    return parse(text, tmp_path, env).checks[0]


def run(spec: Any) -> tuple[Any, Any]:
    outcome = execute(spec)
    return outcome, judge(spec, outcome)


# --- digests


def test_an_image_that_is_current_is_fine(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "latest", digest("web-1"))
    reg.add("acme/web", "latest", manifest=digest("web-1"))
    outcome, core = run(build(tmp_path, docker))
    assert isinstance(outcome, Reading) and outcome.value == 0
    assert (core.level, core.summary) == ("ok", "all 1 image up to date")
    assert ("GET", "/v2/acme/web/tags/list?n=1000") not in reg.requests  # `latest` is digest only
    assert {m for m, _ in reg.requests} == {"HEAD"}


def test_a_rebuilt_tag_is_a_warning(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "latest", digest("web-1"))
    reg.add("acme/web", "latest", manifest=digest("web-2"))
    _, core = run(build(tmp_path, docker))
    assert (core.level, core.value, core.summary) == ("warn", 1, "1 image: web (rebuilt)")


def test_a_multi_arch_tag_compares_the_index_digest(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    """The local RepoDigest of a multi-arch image is the index's. A HEAD that accepts only manifests would answer
    one platform's digest and look like a rebuild; the check asks for indexes too, so it compares like with like."""
    index, platform = digest("index-1"), digest("amd64-1")
    add(docker, reg, "web", "acme/web", "1", index)
    reg.add("acme/web", "1", index=index, manifest=platform)
    _, core = run(build(tmp_path, docker, 'versions = "none"'))
    assert core.level == "ok"
    accept = next(s["accept"] for s in reg.seen if s["method"] == "HEAD")
    for kind in (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ):
        assert kind in accept
    # and a rebuilt index is noticed
    reg.add("acme/web", "1", index=digest("index-2"), manifest=platform)
    assert run(build(tmp_path, docker, 'versions = "none"'))[1].level == "warn"


def test_any_of_several_local_digests_counts(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "latest", digest("old"))
    docker.images[next(iter(docker.images))]["RepoDigests"].append(f"{reg.host}/acme/web@{digest('web-2')}")
    docker.images[next(iter(docker.images))]["RepoDigests"].append("somewhere.else/other@" + digest("x"))
    reg.add("acme/web", "latest", manifest=digest("web-2"))
    assert run(build(tmp_path, docker))[1].level == "ok"


# --- images built here: their base is checked instead


def local_build(docker: FakeDocker, reg: FakeRegistry, *, identity: bool = True, repo_digest: bool = True) -> tuple[str, str]:
    """The captured answers of Docker Desktop 29 (containerd image store) for an image built `FROM python:3.14-slim`
    and for that base, with the base moved to the fake registry. Returns (the built image's id, the base's digest)."""
    captured = fixture("docker_local_build.json")
    built, base = captured["anystatus-localtest:dev"], captured["python:3.14-slim"]
    python = f"{reg.host}/library/python"
    base_digest = base["RepoDigests"][0].split("@")[1]
    docker.add_image(
        base["Id"],
        [f"{python}:3.14-slim"],
        [f"{python}@{base_digest}"],
        base["RootFS"]["Layers"],
        {"Pull": [{"Repository": python}]} if identity else None,
    )
    docker.add_image(
        built["Id"],
        built["RepoTags"],
        built["RepoDigests"] if repo_digest else [],
        built["RootFS"]["Layers"],
        built["Identity"] if identity else None,
    )
    docker.add_container("app", "anystatus-localtest:dev", built["Id"], None)
    return built["Id"], base_digest


def test_a_locally_built_image_is_not_asked_about_by_its_own_name(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    """The bug from the first deployment: with the containerd image store a built image has a RepoDigest
    (`anystatus-localtest@sha256:...`), which made it look like a Docker Hub image ("unknown, registry did not
    answer"). Docker 29's Identity says it was built here, so its base is checked instead."""
    _, base_digest = local_build(docker, reg)
    reg.add("library/python", "3.14-slim", index=base_digest)
    reg.tags = {"library/python": ["3.13-slim", "3.14-slim"]}
    _, core = run(build(tmp_path, docker))
    assert (core.level, core.summary) == ("ok", "all 1 image up to date")
    assert not any("anystatus-localtest" in p for _, p in reg.requests)
    assert ("HEAD", "/v2/library/python/manifests/3.14-slim") in reg.requests


def test_a_rebuilt_base_says_rebuild(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    local_build(docker, reg)
    reg.add("library/python", "3.14-slim", index=digest("python-rebuilt"))
    reg.tags = {"library/python": ["3.14-slim"]}
    _, core = run(build(tmp_path, docker))
    assert (core.level, core.value) == ("warn", 1)
    assert core.summary == f"1 image: anystatus-localtest (base {reg.host}/library/python:3.14-slim rebuilt, rebuild)"


def test_a_newer_base_version(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    _, base_digest = local_build(docker, reg)
    reg.add("library/python", "3.14-slim", index=base_digest)
    reg.tags = {"library/python": ["3.14-slim", "3.15-slim", "4.0-slim", "3.15"]}
    _, core = run(build(tmp_path, docker))
    assert core.summary == f"1 image: anystatus-localtest (base {reg.host}/library/python 3.14 → 3.15)"


def test_without_identity_an_image_without_repo_digests_is_built_here(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    """Docker before 29 with the classic image store: no Identity, and a built image has no RepoDigests."""
    local_build(docker, reg, identity=False, repo_digest=False)
    reg.add("library/python", "3.14-slim", index=digest("python-rebuilt"))
    _, core = run(build(tmp_path, docker, 'versions = "none"'))
    assert core.summary == f"1 image: anystatus-localtest (base {reg.host}/library/python:3.14-slim rebuilt, rebuild)"


def test_the_longest_layer_prefix_is_the_base(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    layers = [digest(f"layer-{i}") for i in range(5)]
    pulled = {"Pull": [{"Repository": f"{reg.host}/library/debian"}]}
    docker.add_image("sha256:" + "d" * 64, [f"{reg.host}/library/debian:13"], [f"{reg.host}/library/debian@{digest('deb')}"], layers[:1], pulled)
    docker.add_image(
        "sha256:" + "e" * 64,
        [f"{reg.host}/library/python:3.14"],
        [f"{reg.host}/library/python@{digest('py')}"],
        layers[:3],
        {"Pull": [{"Repository": f"{reg.host}/library/python"}]},
    )
    docker.add_image("sha256:" + "f" * 64, ["other:1"], ["other@" + digest("o")], layers[:4], {"Build": [{}]})  # built here: not a base
    docker.add_image("sha256:" + "a" * 64, ["mine:dev"], ["mine@" + digest("m")], layers, {"Build": [{}]})
    docker.add_container("mine", "mine:dev", "sha256:" + "a" * 64, None)
    reg.add("library/python", "3.14", index=digest("py"))
    reg.add("library/debian", "13", index=digest("deb-new"))
    _, core = run(build(tmp_path, docker, 'versions = "none"'))
    assert core.summary == "all 1 image up to date"  # python's layers are the longest prefix; debian's rebuild doesn't count


def test_a_pruned_base_is_said_in_the_summary(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built_id, _ = local_build(docker, reg)
    docker.images = {built_id: docker.images[built_id]}  # docker image prune removed python:3.14-slim
    web_id = "sha256:" + digest("web").split(":")[1]
    docker.add_image(
        web_id,
        [f"{reg.host}/acme/web:latest"],
        [f"{reg.host}/acme/web@{digest('web-1')}"],
        [digest("w")],
        {"Pull": [{"Repository": f"{reg.host}/acme/web"}]},
    )
    docker.add_container("web", f"{reg.host}/acme/web:latest", web_id, None)
    reg.add("acme/web", "latest", manifest=digest("web-1"))
    _, core = run(build(tmp_path, docker))
    assert (core.level, core.summary) == ("ok", "all 1 image up to date\nanystatus-localtest: base image not found (pruned?)")
    assert "unknown" not in (core.summary or "")


def test_a_locally_built_image_without_layers_has_no_base(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    docker.add_container("mine", "mine:dev", "sha256:" + "2" * 64, None)
    assert run(build(tmp_path, docker))[1].summary == "no images to check\nmine: base image not found (pruned?)"


# --- versions


@pytest.mark.parametrize(
    ("running", "tags", "versions", "expected"),
    [
        ("1.27.3-alpine", ["1.27.4-alpine", "1.27.2-alpine", "latest"], "minor", "1.27.4-alpine"),
        (
            "1.27.3-alpine",
            ["1.28.0", "1.27.4", "1.27.4-perl", "mainline-alpine"],
            "minor",
            None,
        ),  # other suffix or shape
        ("1.27.3-alpine", ["1.27.4-alpine", "1.28.0-alpine", "2.0.0-alpine"], "minor", "1.28.0-alpine"),
        ("1.27.3-alpine", ["1.27.4-alpine", "1.28.0-alpine", "2.0.0-alpine"], "major", "2.0.0-alpine"),
        ("1.27.3-alpine", ["1.27.4-alpine", "2.0.0-alpine"], "none", None),
        (
            "1.27.3",
            ["1.27", "1.28", "1.27.4.1", "1.27.4", "v1.27.5"],
            "minor",
            "1.27.4",
        ),  # same number of parts only
        ("v2.1", ["v2.2", "2.3", "v3.0"], "minor", "v2.2"),  # the v prefix is part of the shape
        ("11.2", ["11.3", "11.10", "12.0"], "minor", "11.10"),  # numbers, not text
        ("latest", ["1.0"], "major", None),
        ("stable-alpine", ["1.0-alpine"], "major", None),
        ("1.27.3", ["1.27.3", "1.27.2"], "minor", None),  # nothing newer
    ],
)
def test_newest_version(running: str, tags: list[str], versions: str, expected: str | None) -> None:
    assert newest_version(running, tags, versions) == expected


def test_parse_version_tag() -> None:
    assert parse_version_tag("1.27.3-alpine") is not None
    assert parse_version_tag("latest") is None
    assert parse_version_tag("1.2.3rc1") is None


def test_a_newer_version_tag_is_a_warning(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "grafana", "acme/grafana", "11.2", digest("g-1"))
    add(docker, reg, "web", "acme/web", "1.27.3-alpine", digest("w-1"))
    reg.add("acme/grafana", "11.2", manifest=digest("g-1"))  # not rebuilt, but 11.3 exists
    reg.add("acme/web", "1.27.3-alpine", manifest=digest("w-2"))  # rebuilt and 1.27.4 exists
    reg.tags = {
        "acme/grafana": ["10.4", "11.2", "11.3", "12.0"],
        "acme/web": ["1.27.3-alpine", "1.27.4-alpine", "1.28.0"],
    }
    _, core = run(build(tmp_path, docker))
    assert (core.level, core.value) == ("warn", 2)
    assert core.summary == "2 images: grafana 11.2 → 11.3, web 1.27.3 → 1.27.4"
    _, core = run(build(tmp_path, docker, 'versions = "major"'))
    assert core.summary == "2 images: grafana 11.2 → 12.0, web 1.27.3 → 1.27.4"
    _, core = run(build(tmp_path, docker, 'versions = "none"'))
    assert core.summary == "1 image: web (rebuilt)"


def test_the_tag_list_follows_the_pages(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "1.0.0", digest("w"))
    reg.add("acme/web", "1.0.0", manifest=digest("w"))
    reg.tags = {"acme/web": ["0.1.0", "0.2.0", "0.3.0", "1.0.0", "1.0.1", "1.0.9", "1.1.0"]}
    reg.page_size = 2
    _, core = run(build(tmp_path, docker))
    assert core.summary == "1 image: web 1.0.0 → 1.1.0"
    pages = [p for _, p in reg.requests if "/tags/list" in p]
    assert len(pages) == 4 and "last=2" in pages[1] and "last=6" in pages[3]


def test_the_tag_list_is_bounded(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    add(docker, reg, "web", "acme/web", "1.0.0", digest("w"))
    reg.add("acme/web", "1.0.0", manifest=digest("w"))
    reg.tags = {"acme/web": [f"0.{i}.0" for i in range(50)] + ["1.0.1"]}  # the newer tag is on a page we won't read
    reg.page_size = 5
    monkeypatch.setattr(registry_api, "MAX_TAG_PAGES", 3)
    _, core = run(build(tmp_path, docker))
    assert core.level == "ok"
    assert core.summary == "all 1 image up to date\ntag list cut after 3 pages"
    assert len([p for _, p in reg.requests if "/tags/list" in p]) == 3


# --- skipped and ignored


def test_an_image_started_by_digest_is_skipped(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    pinned = f"{reg.host}/acme/web@{digest('web-1')}"
    docker.add_container("web", pinned, "sha256:" + "3" * 64, [pinned], repo_tags=[])
    add(docker, reg, "db", "acme/db", "latest", digest("db-1"))
    reg.add("acme/db", "latest", manifest=digest("db-1"))
    _, core = run(build(tmp_path, docker))
    assert core.level == "ok" and core.summary == "all 1 image up to date\n1 pinned by digest, skipped"
    assert not any("/acme/web/" in p for _, p in reg.requests)


def test_a_container_that_shows_only_an_image_id_uses_the_images_tag(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "latest", digest("web-1"))
    docker.list[0]["Image"] = docker.list[0]["ImageID"]  # the tag moved to a newer local image
    reg.add("acme/web", "latest", manifest=digest("web-9"))
    _, core = run(build(tmp_path, docker))
    assert core.summary == "1 image: web (rebuilt)"


def test_ignore(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "pg", "library/postgres", "16", digest("pg-1"))
    add(docker, reg, "web", "acme/web", "latest", digest("web-1"))
    reg.add("library/postgres", "16", manifest=digest("pg-2"))
    reg.tags = {"library/postgres": ["16"]}
    reg.add("acme/web", "latest", manifest=digest("web-2"))
    assert run(build(tmp_path, docker))[1].value == 2
    assert run(build(tmp_path, docker, 'ignore = ["postgres"]'))[1].summary == "1 image: web (rebuilt)"
    before = len(reg.requests)
    assert run(build(tmp_path, docker, 'ignore = ["acme/web", "postgres:16"]'))[1].summary == "no images to check"
    assert len(reg.requests) == before  # ignored images are not even asked about


# --- registries that fail


def test_one_registry_down_makes_that_image_unknown(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    with FakeRegistry() as down:
        add(docker, reg, "web", "acme/web", "latest", digest("web-1"))
        add(docker, down, "api", "acme/api", "latest", digest("api-1"))
        reg.add("acme/web", "latest", manifest=digest("web-2"))
        down.status = 503
        _, core = run(build(tmp_path, docker))
    assert core.level == "warn" and core.value == 1
    assert core.summary == "1 image: web (rebuilt)\n1 unknown (registry did not answer): api"


def test_one_registry_down_and_nothing_else_to_report(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    with FakeRegistry() as down:
        add(docker, reg, "web", "acme/web", "latest", digest("web-1"))
        add(docker, down, "api", "acme/api", "latest", digest("api-1"))
        reg.add("acme/web", "latest", manifest=digest("web-1"))
        down.status = 500
        _, core = run(build(tmp_path, docker))
    assert core.level == "ok" and core.summary == "all 1 image up to date\n1 unknown (registry did not answer): api"


def test_no_registry_answering_is_unknown(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "latest", digest("web-1"))
    reg.status = 502
    spec = build(tmp_path, docker)
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == "no registry answered"
    assert judge(spec, outcome).level == "unknown"


def test_a_registry_that_is_not_there_is_unknown(tmp_path: Path, docker: FakeDocker) -> None:
    with FakeRegistry() as gone:
        host = gone.host
    docker.add_container("web", f"{host}/acme/web:latest", "sha256:" + "4" * 64, [f"{host}/acme/web@{digest('w')}"])
    outcome = execute(build(tmp_path, docker))
    assert isinstance(outcome, Failure) and outcome.summary == "no registry answered"


def test_docker_unreachable_is_a_failure(tmp_path: Path) -> None:
    with FakeDocker(empty=True) as docker:
        spec = build(tmp_path, docker)
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == "Docker unreachable: connection refused"


def test_no_running_containers(tmp_path: Path, docker: FakeDocker) -> None:
    _, core = run(build(tmp_path, docker))
    assert (core.level, core.summary) == ("ok", "no images to check")


def test_many_updates_are_clipped(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    for i in range(8):
        add(docker, reg, f"c{i}", f"acme/app{i}", "latest", digest(f"a{i}"))
        reg.add(f"acme/app{i}", "latest", manifest=digest(f"b{i}"))
    _, core = run(build(tmp_path, docker))
    assert core.value == 8
    assert core.summary == ("8 images: app0 (rebuilt), app1 (rebuilt), app2 (rebuilt), app3 (rebuilt), app4 (rebuilt), +3 more")


# --- authentication


def test_the_bearer_challenge_flow(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "1.0.0", digest("w"))
    reg.add("acme/web", "1.0.0", manifest=digest("w"))
    reg.tags = {"acme/web": ["1.0.0", "1.0.1"]}
    reg.require_auth = True
    _, core = run(build(tmp_path, docker))
    assert core.summary == "1 image: web 1.0.0 → 1.0.1"
    (token_request,) = reg.token_requests  # one anonymous token, reused for the tag list
    assert token_request["query"] == {"service": "fake-registry", "scope": "repository:acme/web:pull"}
    assert token_request["authorization"] == ""
    assert reg.token not in core.summary


def test_registry_auth_env_goes_to_the_registry_as_basic_auth(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "latest", digest("w"))
    reg.add("acme/web", "latest", manifest=digest("w-new"))
    reg.require_auth = True
    reg.token_needs_basic = True
    extra = f'registry_auth_env = {{ "{reg.host}" = "REG_AUTH" }}'
    spec = build(tmp_path, docker, extra, env={"REG_AUTH": "robot:s3cret-pass"})
    _, core = run(spec)
    assert core.summary == "1 image: web (rebuilt)"
    expected = "Basic " + base64.b64encode(b"robot:s3cret-pass").decode()
    assert reg.token_requests[0]["authorization"] == expected
    assert "s3cret" not in (core.summary or "") and spec.secrets == ("robot:s3cret-pass",)


def test_credentials_are_refused_without_them(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "latest", digest("w"))
    reg.add("acme/web", "latest", manifest=digest("w"))
    reg.require_auth = True
    reg.token_needs_basic = True
    outcome = execute(build(tmp_path, docker))
    assert isinstance(outcome, Failure) and outcome.summary == "no registry answered"


def test_credentials_never_go_to_a_token_service_on_another_host(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "latest", digest("w"))
    reg.add("acme/web", "latest", manifest=digest("w"))
    reg.require_auth = True
    reg.realm = f"http://localhost:{reg.port}/token"  # the registry names another host as its token service
    extra = f'registry_auth_env = {{ "{reg.host}" = "REG_AUTH" }}'
    outcome, core = run(build(tmp_path, docker, extra, env={"REG_AUTH": "robot:s3cret-pass"}))
    assert reg.token_requests and reg.token_requests[0]["authorization"] == ""
    assert core.level == "ok"
    assert "s3cret" not in repr(outcome)


def test_a_failure_summary_never_contains_the_token_or_credentials(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "latest", digest("w"))
    reg.require_auth = True
    reg.token_status = 500  # the token service is broken
    outcome = execute(build(tmp_path, docker, f'registry_auth_env = {{ "{reg.host}" = "A" }}', env={"A": "u:pw-zzz"}))
    assert isinstance(outcome, Failure)
    assert "pw-zzz" not in repr(outcome) and reg.token not in repr(outcome)


# --- names


@pytest.mark.parametrize(
    ("text", "registry", "repository", "tag"),
    [
        ("nginx", "registry-1.docker.io", "library/nginx", "latest"),
        ("nginx:1.27.3-alpine", "registry-1.docker.io", "library/nginx", "1.27.3-alpine"),
        ("grafana/grafana:11.2", "registry-1.docker.io", "grafana/grafana", "11.2"),
        ("docker.io/library/redis:7", "registry-1.docker.io", "library/redis", "7"),
        ("index.docker.io/grafana/grafana", "registry-1.docker.io", "grafana/grafana", "latest"),
        ("ghcr.io/astral-sh/uv:0.13.7", "ghcr.io", "astral-sh/uv", "0.13.7"),
        ("localhost:5000/app", "localhost:5000", "app", "latest"),
        ("registry.example:8443/team/app:2", "registry.example:8443", "team/app", "2"),
        ("python@sha256:" + "a" * 64, "registry-1.docker.io", "library/python", None),
    ],
)
def test_parse_image(text: str, registry: str, repository: str, tag: str | None) -> None:
    ref = parse_image(text)
    assert (ref.registry, ref.repository, ref.tag) == (registry, repository, tag)


def test_parse_image_digest_and_errors() -> None:
    assert parse_image("nginx:1.27@sha256:" + "b" * 64).digest == "sha256:" + "b" * 64
    assert ImageRef("x", "a/b").short_name == "b"
    for bad in ("", "UPPER/case", "a@b", "nginx:", "a b"):
        with pytest.raises(ValueError):
            parse_image(bad)


# --- config


@pytest.mark.parametrize(
    ("extra", "env", "message"),
    [
        ("crit = 2", None, "never critical"),
        ('worse = "below"', None, "'worse' is not used"),
        ('versions = "patch"', None, "'versions' must be one of"),
        ('ignore = "nginx"', None, "'ignore' must be a list"),
        ('registry_auth_env = { "ghcr.io" = "G" }', {"G": "no-colon"}, "must hold user:password"),
        ('registry_auth_env = { "not a host" = "G" }', {"G": "a:b"}, "not a registry host"),
        ("bogus = 1", None, "unknown key 'bogus'"),
    ],
)
def test_config_errors(tmp_path: Path, extra: str, env: dict[str, str] | None, message: str) -> None:
    text = f'[[check]]\nid = "u"\ntype = "docker_updates"\ndocker = "tcp://localhost:2375"\n{extra}\n'
    with pytest.raises(ConfigError) as err:
        parse(text, tmp_path, env)
    assert message in "; ".join(err.value.problems)


def test_warn_can_be_raised_and_a_missing_credential_variable_is_unknown(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    add(docker, reg, "web", "acme/web", "latest", digest("w"))
    reg.add("acme/web", "latest", manifest=digest("w2"))
    _, core = run(build(tmp_path, docker, "warn = 2"))
    assert (core.level, core.value) == ("ok", 1)  # one update is below warn = 2
    spec = build(tmp_path, docker, 'registry_auth_env = { "ghcr.io" = "NOT_SET" }')
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and outcome.summary == "environment variable NOT_SET not set"
