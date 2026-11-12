"""`docker_updates`: do the images of the running containers have an update in their registry?

Per distinct image of a running container (see docs/checker.md):

- *rebuilt*: the registry's digest for the tag (a manifest HEAD) is not one of the local image's RepoDigests. For a
  multi-arch image the registry's answer is the index digest, which is what Docker records as the RepoDigest, so
  like is compared with like.
- *newer version*: the registry has a higher version tag of the same shape (same number of numeric parts, same
  `v` prefix and suffix, e.g. `-alpine`) - within the same major unless `versions = "major"`.

An image started by digest is skipped (pinned on purpose). The value is the number of images with an update, `warn` at
most: `crit` is a config error. One registry failing makes that image unknown; the whole check is `unknown` only if no
registry answered.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from anystatus.checks import registry as registry_api
from anystatus.checks.base import Check, register
from anystatus.checks.docker_api import (
    DockerClient,
    Endpoint,
    NotFound,
    RunningImage,
    docker_endpoint,
    image_info,
    running_images,
)
from anystatus.checks.registry import (
    ImageRef,
    RegistryClient,
    RegistryError,
    parse_image,
)
from anystatus.model import CheckFailure, Reading
from anystatus.table import Table

MAX_NAMES = 5  # images named in the summary; the rest is counted
_VERSION_TAG = re.compile(r"^(v?)(\d+(?:\.\d+)*)(-.+)?$")


@dataclass(frozen=True)
class Shape:
    prefix: str
    parts: int
    suffix: str


def parse_version_tag(tag: str) -> tuple[Shape, tuple[int, ...]] | None:
    """`1.27.3-alpine` -> (Shape("", 3, "-alpine"), (1, 27, 3)); None for a tag that doesn't read as a version."""
    match = _VERSION_TAG.match(tag)
    if not match:
        return None
    numbers = tuple(int(p) for p in match.group(2).split("."))
    return Shape(match.group(1), len(numbers), match.group(3) or ""), numbers


def newest_version(running: str, tags: list[str], versions: str) -> str | None:
    """The highest tag of the same shape as `running` that is newer; None when there is none (or versions = none)."""
    parsed = parse_version_tag(running)
    if parsed is None or versions == "none":
        return None
    shape, numbers = parsed
    best: tuple[int, ...] = numbers
    best_tag: str | None = None
    for tag in tags:
        candidate = parse_version_tag(tag)
        if candidate is None or candidate[0] != shape:
            continue
        other = candidate[1]
        if versions == "minor" and other[0] != numbers[0]:
            continue
        if other > best:
            best, best_tag = other, tag
    return best_tag


@dataclass(frozen=True)
class _Verdict:
    name: str
    state: str  # "current", "rebuilt", "newer", "unknown", "skipped"
    detail: str = ""  # for "newer": "11.2 -> 11.3"
    truncated: bool = False


@register
class DockerUpdates(Check):
    type_name = "docker_updates"
    default_limits = (1.0, None)
    placeholders = frozenset({"names"})

    def __init__(
        self,
        endpoint: Endpoint,
        versions: str,
        ignore: list[str],
        credentials: dict[str, str],
    ) -> None:
        self.client = DockerClient(endpoint)
        self.versions = versions
        self.ignore = {i.lower() for i in ignore}
        self.credentials = credentials

    @classmethod
    def from_config(cls, table: Table) -> DockerUpdates:
        if table.has("crit"):
            table.error(
                "docker_updates is never critical (an update is 'do it this weekend'): 'crit' is not used. image_vulns is the check that turns red"
            )
        if table.has("worse"):
            table.error("docker_updates: 'worse' is not used")
        endpoint = docker_endpoint(table)
        versions = table.choice("versions", ("minor", "major", "none"), "minor")
        ignore = table.str_list("ignore") or []
        credentials: dict[str, str] = {}
        given = table.str_map("registry_auth_env")
        for registry, variable in (given or {}).items():
            try:
                host = parse_image(f"{registry}/x").registry
            except ValueError:
                table.error(f"'registry_auth_env': {registry!r} is not a registry host")
                continue
            secret = table.env_value(variable)
            if secret is None:
                continue
            if ":" not in secret:
                table.error(f"'registry_auth_env': {variable} must hold user:password")
                continue
            credentials[host] = secret
        return cls(endpoint, versions, ignore, credentials)

    def _ignored(self, running: RunningImage, ref: ImageRef | None) -> bool:
        names = {running.reference.lower()}
        if ref is not None:
            names |= {ref.repository, ref.short_name}
            if ref.repository.startswith("library/"):
                names.add(ref.repository[len("library/") :])
            if ref.tag:
                names |= {f"{ref.repository}:{ref.tag}", f"{ref.short_name}:{ref.tag}"}
        return bool(names & self.ignore)

    def run(self, timeout: float) -> Reading:
        deadline = time.monotonic() + timeout

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckFailure(f"timed out after {timeout:g} s")
            return remaining

        images = running_images(self.client, left())
        registry = RegistryClient(self.credentials, deadline)
        verdicts: list[_Verdict] = []
        for image in images:
            verdicts.append(self._check_image(image, registry, left))

        considered = [v for v in verdicts if v.state != "skipped"]
        pinned = sum(1 for v in verdicts if v.state == "skipped" and v.detail == "pinned")
        notes: list[str] = []
        updates = [v for v in considered if v.state in ("rebuilt", "newer")]
        unknown = [v for v in considered if v.state == "unknown"]
        if considered and len(unknown) == len(considered):
            raise CheckFailure("no registry answered")
        if pinned:
            notes.append(f"{pinned} pinned by digest, skipped")
        if unknown:
            notes.append(f"{len(unknown)} unknown (registry did not answer): {_names(unknown)}")
        if any(v.truncated for v in considered):
            notes.append(f"tag list cut after {registry_api.MAX_TAG_PAGES} pages")

        if updates:
            count = len(updates)
            text = f"{count} image{'s' if count != 1 else ''}: {_names(updates)}"
            names = _names(updates)
        else:
            current = len(considered) - len(unknown)
            text = f"all {current} image{'s' if current != 1 else ''} up to date" if current else "no images to check"
            names = ""
        if notes:
            text += "; " + "; ".join(notes)
        return Reading(value=float(len(updates)), text=text, fields={"names": names})

    def _check_image(self, image: RunningImage, registry: RegistryClient, left: Callable[[], float]) -> _Verdict:
        reference = image.reference
        try:
            if not reference:  # the container shows only an id: use a tag the image has
                tags, _ = image_info(self.client, image.image_id, left())
                reference = tags[0] if tags else ""
            ref = parse_image(reference) if reference else None
        except NotFound:
            return _Verdict(image.image_id[:19], "unknown")
        except ValueError:
            ref = None
        label = ref.short_name if ref else image.image_id[7:19]
        if ref is not None and ref.digest is not None:
            return _Verdict(label, "skipped", "pinned")
        if ref is None or self._ignored(image, ref):
            return _Verdict(label, "skipped", "ignored" if ref is not None else "untagged")
        assert ref.tag is not None
        try:
            _, repo_digests = image_info(self.client, image.image_id, left())
            local = {
                digest
                for entry in repo_digests
                if (own := _safe_parse(entry)) is not None
                and (own.registry, own.repository) == (ref.registry, ref.repository)
                and (digest := own.digest)
            }
            if not local:  # built or loaded here: there is no registry digest to compare with
                return _Verdict(label, "skipped", "local")
            current = registry.manifest_digest(ref)
            rebuilt = current not in local
            newer: str | None = None
            truncated = False
            if self.versions != "none" and parse_version_tag(ref.tag) is not None:
                tags, truncated = registry.list_tags(ref)
                newer = newest_version(ref.tag, tags, self.versions)
        except (RegistryError, CheckFailure):
            return _Verdict(label, "unknown")
        if newer is not None:
            return _Verdict(label, "newer", f"{_version_text(ref.tag)} → {_version_text(newer)}", truncated)
        return _Verdict(label, "rebuilt" if rebuilt else "current", truncated=truncated)


def _safe_parse(entry: str) -> ImageRef | None:
    try:
        return parse_image(entry)
    except ValueError:
        return None


def _version_text(tag: str) -> str:
    """The version part of a tag for the summary (`1.27.3-alpine` is shown as `1.27.3`).
    The suffix is the same on both sides, so it is left out."""
    parsed = _VERSION_TAG.match(tag)
    return (parsed.group(1) + parsed.group(2)) if parsed else tag


def _names(verdicts: list[_Verdict]) -> str:
    shown = []
    for v in sorted(verdicts, key=lambda v: v.name)[:MAX_NAMES]:
        if v.state == "newer":
            shown.append(f"{v.name} {v.detail}")
        elif v.state == "rebuilt":
            shown.append(f"{v.name} (rebuilt)")
        else:
            shown.append(v.name)
    if len(verdicts) > MAX_NAMES:
        shown.append(f"+{len(verdicts) - MAX_NAMES} more")
    return ", ".join(shown)
