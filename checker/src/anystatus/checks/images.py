"""Local images as `docker_updates` and `image_vulns` see them: where an image came from, and the base of one built here.

**Registry image or built here?** Docker 29 says so itself: an image's `Identity` is `{"Pull": [{"Repository":
"docker.io/library/python"}]}` for one pulled from a registry and `{"Build": [...]}` for one built here. Older engines
don't have `Identity`; there an image with `RepoDigests` counts as pulled. That is right for Docker's classic image
store, but with the containerd image store (the default of new installs) a locally built image has a RepoDigest too
(`myapp@sha256:...`): on such an engine before Docker 29 it still looks like a registry image.

**The base of an image built here.** Docker records no base name, so the Dockerfile says it:
`LABEL org.opencontainers.image.base.name=python:3.13-slim`. With a tag, the base is that tag and its layers come
from the registry (`RegistryClient.layer_ids`, one HEAD per run while the tag is unchanged); with a digest it is
pinned, and with a tag next to the digest it was rebuilt when the tag's layers are no longer the pinned ones. The
image's first layers are the base's, so a base whose registry layers differ from our first ones was
rebuilt (assuming a rebuilt base kept its number of layers). Without a usable label the base is guessed: the local
registry image whose `RootFS.Layers` are the longest prefix of ours. That fails when the base is no longer in the
image store (`docker image prune`), or was never listed (BuildKit doesn't list a base only the build fetched), or
is an older copy than the build used.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from anystatus.checks.docker_api import DockerClient, RunningImage, image_details, image_list
from anystatus.checks.registry import ImageRef, Platform, RegistryClient, parse_image
from anystatus.table import Table

BASE_LABEL = "org.opencontainers.image.base.name"


@dataclass(frozen=True)
class LocalImage:
    id: str
    tags: tuple[str, ...]  # RepoTags
    digests: tuple[str, ...]  # RepoDigests
    layers: tuple[str, ...]  # RootFS.Layers (diff ids)
    pulled_from: frozenset[tuple[str, str]] | None  # (registry, repository) per Identity.Pull; None: Docker doesn't say
    base_label: str | None  # the base the Dockerfile names; empty or missing: None
    platform: Platform

    @classmethod
    def from_inspect(cls, data: dict[str, Any]) -> LocalImage:
        def strings(value: Any) -> tuple[str, ...]:
            return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()

        rootfs = data.get("RootFS")
        identity = data.get("Identity")
        pulled: frozenset[tuple[str, str]] | None = None
        if isinstance(identity, dict):
            found = set()
            for pull in identity.get("Pull") or []:
                repository = pull.get("Repository") if isinstance(pull, dict) else None
                if isinstance(repository, str) and (ref := safe_parse(repository)) is not None:
                    found.add((ref.registry, ref.repository))
            pulled = frozenset(found)
        config = data.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        label = labels.get(BASE_LABEL) if isinstance(labels, dict) else None
        return cls(
            id=str(data.get("Id", "")),
            tags=strings(data.get("RepoTags")),
            digests=strings(data.get("RepoDigests")),
            layers=strings(rootfs.get("Layers")) if isinstance(rootfs, dict) else (),
            pulled_from=pulled,
            base_label=label.strip() or None if isinstance(label, str) else None,
            platform=Platform(str(data.get("Os") or ""), str(data.get("Architecture") or ""), str(data.get("Variant") or "")),
        )

    def registry_repos(self) -> set[tuple[str, str]]:
        """The (registry, repository) pairs this image is known to come from."""
        if self.pulled_from is not None:
            return set(self.pulled_from)
        return {(ref.registry, ref.repository) for d in self.digests if (ref := safe_parse(d)) is not None}

    def from_registry(self, ref: ImageRef) -> bool:
        """Was this image pulled from `ref`'s repository (rather than built here, or tagged here from another one)?"""
        return (ref.registry, ref.repository) in self.registry_repos()

    def digests_for(self, ref: ImageRef) -> set[str]:
        """The registry digests this image has under `ref`'s repository (multi-arch: the index digest)."""
        return {
            digest
            for entry in self.digests
            if (own := safe_parse(entry)) is not None and (own.registry, own.repository) == (ref.registry, ref.repository) and (digest := own.digest)
        }

    def registry_tag(self) -> ImageRef | None:
        """A tag of this image in a repository it was pulled from, to ask the registry about; None if it has none."""
        repos = self.registry_repos()
        for tag in sorted(self.tags):
            ref = safe_parse(tag)
            if ref is not None and ref.tag and ref.digest is None and (ref.registry, ref.repository) in repos:
                return ref
        return None


@dataclass
class Base:
    """The base of an image built here, named by its label or guessed from the local images."""

    ref: ImageRef  # the tag the registry is asked about (with a digest: pinned)
    built: LocalImage  # the image built here
    local: LocalImage | None = None  # the guessed base, as found in the image store; None: named by the label
    _current: tuple[str, ...] | None = field(default=None, init=False, repr=False)

    @property
    def name(self) -> str:
        """As a user wrote it in FROM: `python:3.14-slim`, `grafana/grafana:11.2`, `ghcr.io/org/app:1`."""
        return display_name(self.ref)

    @property
    def pinned(self) -> bool:
        return self.ref.digest is not None

    def layers(self, registry: RegistryClient) -> tuple[str, ...]:
        """The layers of `built` that came with the base. Raises RegistryError (a label base asks the registry)."""
        if self.local is not None:
            return self.local.layers
        return self.built.layers[: len(self._registry_layers(registry))]

    def rebuilt(self, registry: RegistryClient) -> bool:
        """Has the base been rebuilt since `built` was built? A pinned base: has its tag moved away from the pinned
        digest (never, for a digest without a tag). Raises RegistryError."""
        if self.pinned:
            if self.ref.tag is None:
                return False
            return registry.layer_ids(replace(self.ref, digest=None), self.built.platform) != self._registry_layers(registry)
        if self.local is not None:
            return rebuilt(registry, self.ref, self.local)
        current = self._registry_layers(registry)
        # A rebuilt base is assumed to have kept its number of layers, so our first len(current) layers are the old base's.
        return self.built.layers[: len(current)] != current

    def _registry_layers(self, registry: RegistryClient) -> tuple[str, ...]:
        if self._current is None:
            self._current = registry.layer_ids(self.ref, self.built.platform)
        return self._current


def display_name(ref: ImageRef) -> str:
    if ref.tag is None:
        return f"{ref.repository}@{ref.digest}"
    repository = ref.repository
    if ref.registry == "registry-1.docker.io":
        repository = repository.removeprefix("library/")
        return f"{repository}:{ref.tag}"
    return f"{ref.registry}/{repository}:{ref.tag}"


def safe_parse(entry: str) -> ImageRef | None:
    try:
        return parse_image(entry)
    except ValueError:
        return None


def is_ignored(ignore: set[str], running: RunningImage, ref: ImageRef | None) -> bool:
    """Whether an `ignore` entry (lower case) names this image: as the container was created with, or by repository,
    short name, or either with the tag (`postgres`, `library/postgres`, `postgres:16`)."""
    names = {running.reference.lower()}
    if ref is not None:
        names |= {ref.repository, ref.short_name}
        if ref.repository.startswith("library/"):
            names.add(ref.repository[len("library/") :])
        if ref.tag:
            names |= {f"{ref.repository}:{ref.tag}", f"{ref.short_name}:{ref.tag}"}
    return bool(names & ignore)


class ImageStore:
    """One run's view of the local images: inspected once each, the listing read once."""

    def __init__(self, client: DockerClient, left: Callable[[], float]) -> None:
        self._client = client
        self._left = left
        self._inspected: dict[str, LocalImage] = {}
        self._candidates: list[LocalImage] | None = None

    def inspect(self, image_id: str) -> LocalImage:
        if image_id not in self._inspected:
            self._inspected[image_id] = LocalImage.from_inspect(image_details(self._client, image_id, self._left()))
        return self._inspected[image_id]

    def base_of(self, image: LocalImage) -> Base | None:
        """The base of an image built here: the one its label names, else the guess of `_guess_base`. No registry
        call: `Base` makes those when asked. None when there is no label and no guess."""
        if image.base_label is not None and (ref := safe_parse(image.base_label)) is not None:
            return Base(ref, image)
        return self._guess_base(image)

    def _guess_base(self, image: LocalImage) -> Base | None:
        """The registry image whose layers are the longest prefix of `image`'s (equal layers count: a FROM with no
        layer of its own). None when there is none, e.g. because the base was pruned."""
        if not image.layers:
            return None
        best: Base | None = None
        best_layers = 0
        for candidate in self._registry_images():
            n = len(candidate.layers)
            if not n or n > len(image.layers) or image.layers[:n] != candidate.layers:
                continue
            if candidate.id == image.id and candidate.pulled_from is None:
                continue  # the same image, and Docker can't say where it came from
            tag = candidate.registry_tag()
            if tag is not None and n > best_layers:
                best, best_layers = Base(tag, image, candidate), n
        return best

    def _registry_images(self) -> list[LocalImage]:
        if self._candidates is None:
            listing = image_list(self._client, self._left())
            self._candidates = []
            for item in listing:
                # A base has to come from a registry (RepoDigests) and have a tag to ask it about.
                if item.get("RepoDigests") and any(t != "<none>:<none>" for t in item.get("RepoTags") or []):
                    self._candidates.append(self.inspect(str(item.get("Id", ""))))
        return self._candidates


def registry_credentials(table: Table) -> dict[str, str]:
    """`registry_auth_env = { "ghcr.io" = "GHCR_AUTH" }`: registry host -> "user:password" from the environment."""
    credentials: dict[str, str] = {}
    for registry, variable in (table.str_map("registry_auth_env") or {}).items():
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
    return credentials


def rebuilt(registry: RegistryClient, ref: ImageRef, local: LocalImage) -> bool:
    """Has the registry a newer build of `ref`: is the tag's digest none of the local image's? Raises RegistryError.
    One manifest HEAD, which Docker Hub doesn't count as a pull."""
    return registry.manifest_digest(ref) not in local.digests_for(ref)
