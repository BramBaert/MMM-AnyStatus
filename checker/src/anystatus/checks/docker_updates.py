"""`docker_updates`: do the images of the running containers have an update in their registry?

Per distinct image of a running container (see docs/checker.md):

- *rebuilt*: the registry's digest for the tag (a manifest HEAD) is not one of the local image's RepoDigests. For a
  multi-arch image the registry's answer is the index digest, which is what Docker records as the RepoDigest, so
  like is compared with like.
- *newer version*: the registry has a higher version tag of the same shape (same number of numeric parts, same
  `v` prefix and suffix, e.g. `-alpine`) - within the same major unless `versions = "major"`.

An image built here is checked through its base image instead (found from the layers, see images.py): "myapp (base
python:3.14-slim rebuilt, rebuild)". If its base is no longer in the image store, the summary says so.

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
from anystatus.checks.images import ImageStore, rebuilt, registry_credentials
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
    state: str  # "current", "rebuilt", "newer", "unknown", "skipped", "no base"
    detail: str = ""  # for "newer": "11.2 -> 11.3"; for "skipped": why
    truncated: bool = False
    base: str = ""  # an image built here: its base (`python:3.14-slim`), which is what was checked


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
        return cls(endpoint, versions, ignore, registry_credentials(table))

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
        store = ImageStore(self.client, left)
        verdicts: list[_Verdict] = []
        for image in images:
            verdicts.append(self._check_image(image, registry, store, left))

        considered = [v for v in verdicts if v.state not in ("skipped", "no base")]
        pinned = sum(1 for v in verdicts if v.state == "skipped" and v.detail == "pinned")
        no_base = [v for v in verdicts if v.state == "no base"]
        notes: list[str] = []
        updates = [v for v in considered if v.state in ("rebuilt", "newer")]
        unknown = [v for v in considered if v.state == "unknown"]
        if considered and len(unknown) == len(considered):
            raise CheckFailure("no registry answered")
        if pinned:
            notes.append(f"{pinned} pinned by digest, skipped")
        if no_base:
            notes.append(f"{_names(no_base)}: base image not found (pruned?)")
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

    def _check_image(self, image: RunningImage, registry: RegistryClient, store: ImageStore, left: Callable[[], float]) -> _Verdict:
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
        try:
            local = store.inspect(image.image_id)
            base_name = ""
            if not local.from_registry(ref):
                # Built here (or tagged here from another image): its registry is the base's. See images.py.
                base = store.find_base(local)
                if base is None:
                    return _Verdict(label, "no base")
                ref, local, base_name = base.ref, base.image, base.name
            assert ref.tag is not None
            was_rebuilt = rebuilt(registry, ref, local)
            newer: str | None = None
            truncated = False
            if self.versions != "none" and parse_version_tag(ref.tag) is not None:
                tags, truncated = registry.list_tags(ref)
                newer = newest_version(ref.tag, tags, self.versions)
        except (RegistryError, CheckFailure):  # NotFound (an image that went away) included
            return _Verdict(label, "unknown")
        if newer is not None:
            return _Verdict(label, "newer", f"{_version_text(ref.tag)} → {_version_text(newer)}", truncated, base_name)
        return _Verdict(label, "rebuilt" if was_rebuilt else "current", truncated=truncated, base=base_name)


def _version_text(tag: str) -> str:
    """The version part of a tag for the summary (`1.27.3-alpine` is shown as `1.27.3`).
    The suffix is the same on both sides, so it is left out."""
    parsed = _VERSION_TAG.match(tag)
    return (parsed.group(1) + parsed.group(2)) if parsed else tag


def _names(verdicts: list[_Verdict]) -> str:
    shown = []
    for v in sorted(verdicts, key=lambda v: v.name)[:MAX_NAMES]:
        if v.base and v.state == "newer":
            shown.append(f"{v.name} (base {v.base.rsplit(':', 1)[0]} {v.detail})")
        elif v.base and v.state == "rebuilt":
            shown.append(f"{v.name} (base {v.base} rebuilt, rebuild)")
        elif v.state == "newer":
            shown.append(f"{v.name} {v.detail}")
        elif v.state == "rebuilt":
            shown.append(f"{v.name} (rebuilt)")
        else:
            shown.append(v.name)
    if len(verdicts) > MAX_NAMES:
        shown.append(f"+{len(verdicts) - MAX_NAMES} more")
    return ", ".join(shown)
