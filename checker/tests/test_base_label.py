"""The base of an image built here, named by the `org.opencontainers.image.base.name` label: its layers come from the
registry (checks/registry.py `layer_ids`, checks/images.py `Base`)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import FakeDocker, FakeRegistry, digest, trivy_calls, trivy_json, write_fake_trivy
from test_docker_updates import build as updates_build
from test_docker_updates import run as updates_run
from test_image_vulns import SCHEDULE, rows
from test_image_vulns import build as vulns_build
from test_image_vulns import run as vulns_run

from anystatus.checks import registry as registry_api
from anystatus.checks.registry import Platform, RegistryClient, RegistryError, parse_image, redirect_target
from anystatus.model import Failure

BASE = [digest("base-0"), digest("base-1")]
OWN = digest("own-0")
BUILT_ID = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def fresh_cache() -> Iterator[None]:
    registry_api._LAYER_CACHE.clear()
    yield
    registry_api._LAYER_CACHE.clear()


@pytest.fixture
def docker() -> Iterator[FakeDocker]:
    with FakeDocker(empty=True) as fake:
        yield fake


@pytest.fixture
def reg() -> Iterator[FakeRegistry]:
    with FakeRegistry() as fake:
        yield fake


def built(docker: FakeDocker, label: str | None, layers: list[str] | None = None, variant: str = "") -> None:
    """A running container of mine:1, built here, whose Dockerfile named its base."""
    docker.add_image(BUILT_ID, ["mine:1"], [], layers or [*BASE, OWN], {"Build": [{}]}, base_label=label, variant=variant)
    docker.add_container("mine", "mine:1", BUILT_ID, None)


def head_and_get(reg: FakeRegistry) -> tuple[int, int]:
    return sum(1 for m, _ in reg.requests if m == "HEAD"), sum(1 for m, _ in reg.requests if m == "GET")


# --- docker_updates


def test_a_current_label_base_is_fine(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built(docker, f"{reg.host}/library/python:3.13-slim")
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE})
    _, core = updates_run(updates_build(tmp_path, docker, 'versions = "none"'))
    assert (core.level, core.summary) == ("ok", "all 1 image up to date")
    assert head_and_get(reg) == (1, 3)  # HEAD of the tag, then the index, the platform's manifest and the config


def test_a_rebuilt_label_base_says_rebuild(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built(docker, f"{reg.host}/library/python:3.13-slim")
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": [digest("base-0-new"), digest("base-1-new")]})
    _, core = updates_run(updates_build(tmp_path, docker, 'versions = "none"'))
    assert core.summary == f"1 image: mine (base {reg.host}/library/python:3.13-slim rebuilt, rebuild)"


def test_an_unchanged_tag_costs_one_head_per_run(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built(docker, f"{reg.host}/library/python:3.13-slim")
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE})
    spec = updates_build(tmp_path, docker, 'versions = "none"')
    updates_run(spec)
    before = len(reg.requests)
    updates_run(spec)
    assert reg.requests[before:] == [("HEAD", "/v2/library/python/manifests/3.13-slim")]
    # a rebuilt tag has a new digest: read again
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": [digest("x"), digest("y")]})
    before = len(reg.requests)
    _, core = updates_run(spec)
    assert core.level == "warn" and head_and_get(reg)[1] == 6


def test_an_empty_label_falls_back_to_the_local_guess(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    python = f"{reg.host}/library/python"
    docker.add_image("sha256:" + "b" * 64, [f"{python}:3.13-slim"], [f"{python}@{digest('py')}"], BASE, {"Pull": [{"Repository": python}]})
    built(docker, "")
    reg.add("library/python", "3.13-slim", index=digest("py"))
    _, core = updates_run(updates_build(tmp_path, docker, 'versions = "none"'))
    assert core.summary == "all 1 image up to date"
    assert head_and_get(reg) == (1, 0)


def test_a_label_that_is_not_an_image_name_falls_back_too(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built(docker, "Not A Name!")
    _, core = updates_run(updates_build(tmp_path, docker))
    assert "mine: base image not found" in core.summary


def test_a_pinned_label_base_is_skipped_like_a_pinned_image(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built(docker, f"{reg.host}/library/python:3.13-slim@{digest('pinned')}")
    _, core = updates_run(updates_build(tmp_path, docker))
    assert core.summary == "no images to check\n1 pinned by digest, skipped"
    assert reg.requests == []


def test_a_registry_error_makes_the_verdict_unknown(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built(docker, f"{reg.host}/library/python:3.13-slim")
    reg.add("library/python", "3.13-slim", index=digest("i"))  # the HEAD works; the manifest GET finds nothing
    outcome, _ = updates_run(updates_build(tmp_path, docker, 'versions = "none"'))
    assert isinstance(outcome, Failure)


# --- image_vulns


def test_a_cve_in_a_base_layer_waits_while_the_base_is_current(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built(docker, f"{reg.host}/library/python:3.13-slim")
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE})
    command = write_fake_trivy(tmp_path, {"mine:1": trivy_json([("CVE-2026-6001", "HIGH", "1.1")], layer=BASE[1])})
    spec = vulns_build(tmp_path, docker, command, SCHEDULE)
    vulns_run(spec)
    assert rows(spec) == [("mine", "CVE-2026-6001", "waiting", "warn")]
    assert len(trivy_calls(tmp_path)) == 1


def test_a_rebuilt_base_that_fixes_it_makes_it_rebuild(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built(docker, f"{reg.host}/library/python:3.13-slim")
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": [digest("n0"), digest("n1")]})
    vulns = trivy_json([("CVE-2026-6001", "HIGH", "1.1"), ("CVE-2026-6002", "HIGH", "1.1")], layer=BASE[1])
    outputs = {"mine:1": vulns, f"remote:{reg.host}/library/python:3.13-slim": trivy_json([("CVE-2026-6002", "HIGH", "1.1")])}
    spec = vulns_build(tmp_path, docker, write_fake_trivy(tmp_path, outputs), SCHEDULE)
    vulns_run(spec)
    assert sorted((c, k) for _, c, k, _ in rows(spec)) == [("CVE-2026-6001", "rebuild"), ("CVE-2026-6002", "waiting")]


def test_a_cve_in_the_images_own_layer_is_rebuild(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built(docker, f"{reg.host}/library/python:3.13-slim")
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE})
    command = write_fake_trivy(tmp_path, {"mine:1": trivy_json([("CVE-2026-6003", "CRITICAL", "2.0")], layer=OWN)})
    spec = vulns_build(tmp_path, docker, command, SCHEDULE)
    vulns_run(spec)
    assert rows(spec) == [("mine", "CVE-2026-6003", "rebuild", "warn")]


def test_two_images_on_one_base_tag_are_judged_each_by_its_own_layers(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    # mine (first) is on the current base, old on an older build of the same tag: the first's "not rebuilt" must not
    # decide for the second
    built(docker, f"{reg.host}/library/python:3.13-slim")
    old_base = [digest("old-0"), digest("old-1")]
    old_id = "sha256:" + "b" * 64
    docker.add_image(old_id, ["old:1"], [], [*old_base, digest("own-1")], {"Build": [{}]}, base_label=f"{reg.host}/library/python:3.13-slim")
    docker.add_container("old", "old:1", old_id, None)
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE})
    outputs = {
        "mine:1": trivy_json([("CVE-2026-6001", "HIGH", "1.1")], layer=BASE[1]),
        "old:1": trivy_json([("CVE-2026-6001", "HIGH", "1.1")], layer=old_base[1]),
        f"remote:{reg.host}/library/python:3.13-slim": trivy_json([]),
    }
    spec = vulns_build(tmp_path, docker, write_fake_trivy(tmp_path, outputs), SCHEDULE)
    vulns_run(spec)
    assert sorted((name, kind) for name, _, kind, _ in rows(spec)) == [("mine", "waiting"), ("old", "rebuild")]
    assert head_and_get(reg)[0] == 1  # the tag's digest is asked once for both images


def test_a_pinned_label_base_waits(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    pinned = reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE})
    built(docker, f"{reg.host}/library/python:3.13-slim@{pinned}")
    command = write_fake_trivy(
        tmp_path,
        {"mine:1": trivy_json([("CVE-2026-6001", "HIGH", "1.1")], layer=BASE[0])},
    )
    spec = vulns_build(tmp_path, docker, command, SCHEDULE)
    vulns_run(spec)
    assert rows(spec) == [("mine", "CVE-2026-6001", "waiting", "warn")]
    assert head_and_get(reg)[0] == 0  # a digest needs no HEAD


def test_a_registry_that_fails_makes_everything_waiting_and_says_so(tmp_path: Path, docker: FakeDocker, reg: FakeRegistry) -> None:
    built(docker, f"{reg.host}/library/python:3.13-slim")
    reg.status = 503
    command = write_fake_trivy(tmp_path, {"mine:1": trivy_json([("CVE-2026-6001", "HIGH", "1.1")], layer=BASE[1])})
    spec = vulns_build(tmp_path, docker, command, SCHEDULE)
    vulns_run(spec)
    assert rows(spec) == [("mine", "CVE-2026-6001", "waiting", "warn")]
    assert spec.check.report.notes == ("newer build not checked: mine",)


# --- the registry client


def layers_of(reg: FakeRegistry, platform: Platform, tag: str = "3.13-slim") -> tuple[str, ...]:
    return RegistryClient().layer_ids(parse_image(f"{reg.host}/library/python:{tag}"), platform)


def test_the_platform_is_picked_from_an_index_and_attestations_are_skipped(reg: FakeRegistry) -> None:
    amd, arm = [digest("amd")], [digest("arm")]
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": amd, "linux/arm64": arm}, attestation=True)
    assert layers_of(reg, Platform("linux", "amd64")) == tuple(amd)
    assert layers_of(reg, Platform("linux", "arm64")) == tuple(arm)
    with pytest.raises(RegistryError, match="no image for this platform"):
        layers_of(reg, Platform("unknown", "unknown"))
    with pytest.raises(RegistryError, match="no image for this platform"):
        layers_of(reg, Platform("linux", "riscv64"))


def test_the_variant_is_matched_when_the_local_image_has_one(reg: FakeRegistry) -> None:
    v6, v7 = [digest("v6")], [digest("v7")]
    reg.add_image("library/python", "3.13-slim", {"linux/arm/v6": v6, "linux/arm/v7": v7})
    assert layers_of(reg, Platform("linux", "arm", "v7")) == tuple(v7)
    assert layers_of(reg, Platform("linux", "arm", "v6")) == tuple(v6)
    assert layers_of(reg, Platform("linux", "arm")) == tuple(v6)  # none named: the first that matches


def test_a_single_manifest_tag_needs_no_platform_pick(reg: FakeRegistry) -> None:
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE}, multi_arch=False)
    assert layers_of(reg, Platform("linux", "amd64")) == tuple(BASE)


def test_a_blob_redirect_is_followed_once_without_the_token(reg: FakeRegistry) -> None:
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE})
    reg.require_auth = True
    with FakeRegistry() as cdn:
        for (_, blob), content in reg.blobs.items():
            cdn.cdn_blobs[f"/cdn/{blob}"] = content
        reg.blob_location = f"http://{cdn.host}"
        assert layers_of(reg, Platform("linux", "amd64")) == tuple(BASE)
        assert [s["authorization"] for s in cdn.seen] == [""]
    blob_request = next(s for s in reg.seen if "/blobs/" in s["path"])
    assert blob_request["authorization"] == f"Bearer {reg.token}"


def test_a_second_redirect_is_an_error(reg: FakeRegistry) -> None:
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE})
    with FakeRegistry() as cdn:
        reg.blob_location = f"http://{cdn.host}"
        for _, blob in reg.blobs:
            cdn.cdn_redirects[f"/cdn/{blob}"] = f"http://{cdn.host}/elsewhere"
        with pytest.raises(RegistryError, match="redirected twice"):
            layers_of(reg, Platform("linux", "amd64"))


def test_a_redirect_to_plain_http_is_refused(reg: FakeRegistry) -> None:
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE})
    reg.blob_location = "http://cdn.example.com"
    with pytest.raises(RegistryError, match="not https"):
        layers_of(reg, Platform("linux", "amd64"))


def test_which_redirects_are_followed() -> None:
    assert redirect_target("ghcr.io", "/v2/x/blobs/sha", "https://cdn.example.com/b?sig=1") == ("cdn.example.com", "/b?sig=1")
    assert redirect_target("ghcr.io", "/v2/x/blobs/sha", "/other") == ("ghcr.io", "/other")
    assert redirect_target("ghcr.io", "/v2/x/blobs/sha", "http://127.0.0.1:5000/b") == ("127.0.0.1:5000", "/b")
    for bad in ("http://cdn.example.com/b", "ftp://cdn.example.com/b", "https://user@cdn.example.com/b", ""):
        with pytest.raises(RegistryError):
            redirect_target("ghcr.io", "/v2/x/blobs/sha", bad)


def test_a_config_that_is_too_large_is_refused(reg: FakeRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    reg.add_image("library/python", "3.13-slim", {"linux/amd64": BASE})
    monkeypatch.setattr(registry_api, "MAX_CONFIG", 50)
    with pytest.raises(RegistryError, match="too large"):
        layers_of(reg, Platform("linux", "amd64"))


def test_the_cache_is_bounded(reg: FakeRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_api, "MAX_CACHED_LAYERS", 2)
    for tag in ("a", "b", "c"):
        reg.add_image("library/python", tag, {"linux/amd64": [digest(tag)]})
        layers_of(reg, Platform("linux", "amd64"), tag)
    assert len(registry_api._LAYER_CACHE) == 2
