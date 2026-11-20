"""`docker_updates`: do the images of the running containers have an update in their registry?

Per distinct image of a running container (see docs/checker.md):

- *rebuilt*: the registry's digest for the tag (a manifest HEAD) is not one of the local image's RepoDigests. For a
  multi-arch image the registry's answer is the index digest, which is what Docker records as the RepoDigest, so
  like is compared with like.
- *newer version*: the registry has a higher version tag of the same shape (same number of numeric parts, same
  `v` prefix and suffix, e.g. `-alpine`): any newer one by default, within the same major with `versions = "minor"`.
  A `skip` entry hides a version (and older ones) that was deliberately not taken, until its end date.

An image built here is checked through its base image instead (named by its label, else guessed from the layers, see
images.py): "myapp (base python:3.14-slim rebuilt, rebuild)". If there is no label and the base is no longer in the
image store, the summary says so.

An image (or a base label) pinned by digest is checked through its tag: "web (rebuilt, pinned)" when the tag moved
away from the pin. A digest without a tag has nothing to compare with and is skipped. The value is the number of
images with an update, `warn` at most: `crit` is a config error. One registry failing makes that image unknown; the
whole check is `unknown` only if no registry answered.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime

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
from anystatus.checks.images import ImageStore, display_name, is_ignored, rebuilt, registry_credentials
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


def _today() -> date:
    return datetime.now(UTC).date()


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


def newest_version(running: str, tags: list[str], versions: str, skip_up_to: tuple[int, ...] | None = None) -> str | None:
    """The highest tag of the same shape as `running` that is newer, and newer than `skip_up_to` when given; None when
    there is none (or versions = none)."""
    parsed = parse_version_tag(running)
    if parsed is None or versions == "none":
        return None
    shape, numbers = parsed
    best: tuple[int, ...] = max(numbers, skip_up_to) if skip_up_to is not None else numbers
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
class Skip:
    """One `skip` entry: newer versions of `image` up to `version` are not reported, through the day `until` (None:
    no end)."""

    image: str
    version: str
    numbers: tuple[int, ...]
    until: date | None
    why: str

    def active(self, today: date) -> bool:
        return self.until is None or self.until >= today

    def describe(self) -> str:
        return f"{self.image} {self.version} " + (f"until {self.until.isoformat()}" if self.until else "without end")


def read_skip(table: Table) -> list[Skip]:
    """`skip = [{ image, version, until, why }, ...]`, every key required; `until` is a date, or 0 for no end. Entries
    are numbered from 1 in messages. An `until` that has passed is a warning: the skip no longer counts."""
    raw = table.raw("skip")
    if raw is None:
        return []
    example = '[{ image = "mariadb", version = "11.8", until = 2027-03-01, why = "..." }]'
    if not isinstance(raw, list) or not all(isinstance(entry, dict) for entry in raw):
        table.error(f"'skip' must be a list of tables, such as {example}")
        return []
    skips: list[Skip] = []
    for number, entry in enumerate(raw, start=1):
        where = f"'skip' entry {number}"
        missing = [key for key in ("image", "version", "until", "why") if key not in entry]
        unknown = sorted(set(entry) - {"image", "version", "until", "why"})
        if missing or unknown:
            table.error(f"{where}: " + "; ".join([*(f"missing '{k}'" for k in missing), *(f"unknown key '{k}'" for k in unknown)]))
            continue
        texts = {key: entry[key] for key in ("image", "version", "why")}
        if not all(isinstance(v, str) and v.strip() for v in texts.values()):
            table.error(f"{where}: 'image', 'version' and 'why' must be non-empty text")
            continue
        parsed = parse_version_tag(texts["version"].strip())
        if parsed is None:
            table.error(f"{where}: 'version' must read as a version, such as \"11.8\"")
            continue
        until = entry["until"]
        if isinstance(until, str) and len(until) == 10:
            with contextlib.suppress(ValueError):  # not a date: reported just below
                until = date.fromisoformat(until)
        if until == 0 and not isinstance(until, bool):
            until = None
        elif isinstance(until, datetime) or not isinstance(until, date):
            table.error(f"{where}: 'until' must be a date, such as 2027-03-01, or 0 for a skip without end")
            continue
        skip = Skip(texts["image"].strip(), texts["version"].strip(), parsed[1], until, texts["why"].strip())
        skips.append(skip)
        if not skip.active(_today()):
            table.warning(f"skip entry {number} ({skip.image} {skip.version}): until {until} has passed: remove it")
    return skips


@dataclass(frozen=True)
class _Verdict:
    name: str
    state: str  # "current", "rebuilt", "newer", "unknown", "skipped", "no base"
    detail: str = ""  # for "newer": "11.2 -> 11.3"; for "skipped": why
    truncated: bool = False
    base: str = ""  # an image built here: its base (`python:3.14-slim`), which is what was checked
    pinned: bool = False  # the running image (or its base) is pinned by digest: checked through its tag
    skips_used: frozenset[int] = field(default_factory=frozenset)  # the `skip` entries (by position) that hid a version


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
        skip: list[Skip] | None = None,
    ) -> None:
        self.client = DockerClient(endpoint)
        self.versions = versions
        self.ignore = {i.lower() for i in ignore}
        self.credentials = credentials
        self.skip = skip or []

    @classmethod
    def from_config(cls, table: Table) -> DockerUpdates:
        if table.has("crit"):
            table.error(
                "docker_updates is never critical (an update is 'do it this weekend'): 'crit' is not used. image_vulns is the check that turns red"
            )
        if table.has("worse"):
            table.error("docker_updates: 'worse' is not used")
        endpoint = docker_endpoint(table)
        versions = table.choice("versions", ("major", "minor", "none"), "major")
        ignore = table.str_list("ignore") or []
        skip = read_skip(table)
        return cls(endpoint, versions, ignore, registry_credentials(table), skip)

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
            notes.append(f"{pinned} pinned by digest without a tag, skipped")
        if no_base:
            notes.append(f"{_names(no_base)}: base image not found (pruned?)")
        if unknown:
            notes.append(f"{len(unknown)} unknown (registry did not answer): {_names(unknown)}")
        if any(v.truncated for v in considered):
            notes.append(f"tag list cut after {registry_api.MAX_TAG_PAGES} pages")
        used = frozenset().union(*(v.skips_used for v in verdicts))
        today = _today()
        for index, skip in enumerate(self.skip):
            if not skip.active(today):
                continue
            if index in used:
                notes.append(f"skipped: {skip.describe()}")
            elif not unknown:  # a registry that didn't answer may be why it hid nothing
                notes.append(f"skip of {skip.image} {skip.version} hid nothing: remove it")

        if updates:
            count = len(updates)
            text = f"{count} image{'s' if count != 1 else ''}: {_names(updates)}"
            names = _names(updates)
        else:
            current = len(considered) - len(unknown)
            text = f"all {current} image{'s' if current != 1 else ''} up to date" if current else "no images to check"
            names = ""
        if notes:
            text += "\n" + "\n".join(notes)
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
        if ref is not None and ref.digest is not None and ref.tag is None:
            return _Verdict(label, "skipped", "pinned")
        if ref is None or is_ignored(self.ignore, image, ref):
            return _Verdict(label, "skipped", "ignored" if ref is not None else "untagged")
        running_ref = ref
        pinned = ref.digest is not None
        try:
            local = store.inspect(image.image_id)
            base_name = ""
            if local.from_registry(ref):
                ref = replace(ref, digest=None)  # a pin: the tag is what the registry is asked about
                was_rebuilt = rebuilt(registry, ref, local)
            else:
                # Built here (or tagged here from another image): its registry is the base's. See images.py.
                base = store.base_of(local)
                if base is None:
                    return _Verdict(label, "no base")
                if base.pinned and base.ref.tag is None:
                    return _Verdict(label, "skipped", "pinned")
                was_rebuilt = base.rebuilt(registry)
                pinned = base.pinned
                ref = replace(base.ref, digest=None)
                base_name = display_name(ref)
            assert ref.tag is not None
            newer: str | None = None
            truncated = False
            used: frozenset[int] = frozenset()
            if self.versions != "none" and parse_version_tag(ref.tag) is not None:
                tags, truncated = registry.list_tags(ref)
                skips = self._skips_for(image, running_ref, ref)
                newer = newest_version(ref.tag, tags, self.versions, max((self.skip[i].numbers for i in skips), default=None))
                # A skip hid something when a newer version at or below it exists
                used = frozenset(i for i in skips if newest_version(ref.tag, _up_to(tags, self.skip[i].numbers), self.versions) is not None)
        except (RegistryError, CheckFailure):  # NotFound (an image that went away) included
            return _Verdict(label, "unknown")
        if newer is not None:
            return _Verdict(label, "newer", f"{_version_text(ref.tag)} → {_version_text(newer)}", truncated, base_name, pinned, used)
        return _Verdict(label, "rebuilt" if was_rebuilt else "current", truncated=truncated, base=base_name, pinned=pinned, skips_used=used)

    def _skips_for(self, image: RunningImage, running: ImageRef, checked: ImageRef) -> list[int]:
        """The active `skip` entries (by position) that name this image, or for an image built here its base."""
        today = _today()
        return [
            i
            for i, skip in enumerate(self.skip)
            if skip.active(today) and (is_ignored({skip.image.lower()}, image, running) or is_ignored({skip.image.lower()}, image, checked))
        ]


def _up_to(tags: list[str], numbers: tuple[int, ...]) -> list[str]:
    """The version tags at or below `numbers`."""
    return [tag for tag in tags if (parsed := parse_version_tag(tag)) is not None and parsed[1] <= numbers]


def _version_text(tag: str) -> str:
    """The version part of a tag for the summary (`1.27.3-alpine` is shown as `1.27.3`).
    The suffix is the same on both sides, so it is left out."""
    parsed = _VERSION_TAG.match(tag)
    return (parsed.group(1) + parsed.group(2)) if parsed else tag


def _names(verdicts: list[_Verdict]) -> str:
    shown = []
    for v in sorted(verdicts, key=lambda v: v.name)[:MAX_NAMES]:
        pin = ", pinned" if v.pinned else ""
        if v.base and v.state == "newer":
            shown.append(f"{v.name} (base {v.base.rsplit(':', 1)[0]} {v.detail})")
        elif v.base and v.state == "rebuilt":
            shown.append(f"{v.name} (base {v.base} rebuilt{pin}, rebuild)")
        elif v.state == "newer":
            shown.append(f"{v.name} {v.detail}")
        elif v.state == "rebuilt":
            shown.append(f"{v.name} (rebuilt{pin})")
        else:
            shown.append(v.name)
    if len(verdicts) > MAX_NAMES:
        shown.append(f"+{len(verdicts) - MAX_NAMES} more")
    return ", ".join(shown)
