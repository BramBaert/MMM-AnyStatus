"""A small container registry client (the v2 API) for `docker_updates`: image names, manifest digests, tag lists.

Standard library only. Reads only: HEAD for a tag's digest, GET for tags and tokens, and (`layer_ids`) GET for a
manifest or index and for an image config blob. Redirects are not followed, except one from a blob GET (Docker Hub and
ghcr.io send blobs from a CDN): to an https URL only, and without the Authorization header. The usual Bearer
challenge flow: a 401 carries `WWW-Authenticate: Bearer realm=..., service=..., scope=...`, the token comes from the
realm (anonymously, or with the configured credentials), and the request is repeated once with it.

Docker Hub counts a manifest GET as a pull but not a HEAD, so `layer_ids` asks the tag's digest with a HEAD first and
remembers the layers per digest (process-wide: checks run in their own threads and runs repeat).

Nothing secret (token, credentials, URLs) ever goes into an error text.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urljoin, urlsplit

from anystatus.errors import describe_exception

HUB_REGISTRY = "registry-1.docker.io"
HUB_AUTH_HOST = "auth.docker.io"
_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
PAGE_SIZE = 1000  # tags asked for per page (`n`)
MAX_TAG_PAGES = 10  # the tag list is read for at most this many pages
MAX_BODY = 4_000_000  # tag lists, manifests, indexes
MAX_CONFIG = 1_000_000  # an image config blob
MAX_CACHED_LAYERS = 64
_REDIRECTS = (301, 302, 303, 307, 308)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')
_LINK_NEXT = re.compile(r'<([^>]*)>\s*;\s*rel="?next"?', re.IGNORECASE)


class RegistryError(Exception):
    """A registry could not answer. `summary` is short and free of secrets."""

    def __init__(self, summary: str, detail: str | None = None) -> None:
        super().__init__(summary)
        self.summary = summary
        self.detail = detail


@dataclass(frozen=True)
class ImageRef:
    registry: str  # the host (with a port if there is one) that serves the API; Docker Hub is registry-1.docker.io
    repository: str  # "library/nginx"
    tag: str | None = None
    digest: str | None = None  # an `@sha256:` reference: pinned on purpose

    @property
    def short_name(self) -> str:
        return self.repository.rsplit("/", 1)[-1]


def parse_image(reference: str) -> ImageRef:
    """`nginx` -> registry-1.docker.io/library/nginx:latest, `ghcr.io/org/app:1.2` as it is. Raises ValueError."""
    rest = reference.strip()
    digest: str | None = None
    if "@" in rest:
        rest, _, digest = rest.partition("@")
        if not _DIGEST.match(digest):
            raise ValueError("not a digest")
    first, slash, remainder = rest.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, path = first.lower(), remainder
    else:
        registry, path = HUB_REGISTRY, rest
    tag: str | None = None
    head, colon, after = path.rpartition(":")
    if colon and "/" not in after:
        path, tag = head, after
    if tag == "" or not path or not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*", path):
        raise ValueError("not an image name")
    if registry in _HUB_ALIASES:
        registry = HUB_REGISTRY
    if registry == HUB_REGISTRY and "/" not in path:
        path = f"library/{path}"
    if tag is None and digest is None:
        tag = "latest"
    return ImageRef(registry, path, tag, digest)


@dataclass(frozen=True)
class Platform:
    os: str
    architecture: str
    variant: str = ""  # empty: any


# (registry, repository, manifest digest, platform) -> the image's rootfs.diff_ids. A digest never changes its content.
_LAYER_CACHE: dict[tuple[str, str, str, Platform], tuple[str, ...]] = {}
_LAYER_LOCK = threading.Lock()


def _is_local(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name == "localhost" or name.startswith("127.") or name == "[::1]"


def redirect_target(host: str, path: str, location: str) -> tuple[str, str]:
    """(host, path and query) a blob redirect points to. Only https (plain http only to this machine, as for token
    services), and no user info. Raises RegistryError."""
    target = urlsplit(urljoin(f"https://{host}{path}", location))
    if not location or not target.netloc or "@" in target.netloc:
        raise RegistryError("the registry's redirect address is not valid")
    if target.scheme != "https" and not (target.scheme == "http" and _is_local(target.netloc)):
        raise RegistryError("the registry's redirect is not https")
    return target.netloc, (target.path or "/") + (f"?{target.query}" if target.query else "")


def _pick_platform(index: dict[str, object], platform: Platform) -> str:
    """The digest of the index entry for `platform`. Attestations (platform "unknown") never match."""
    entries = index.get("manifests")
    for entry in entries if isinstance(entries, list) else []:
        info = entry.get("platform") if isinstance(entry, dict) else None
        digest = entry.get("digest") if isinstance(entry, dict) else None
        if not isinstance(info, dict) or not isinstance(digest, str) or not _DIGEST.match(digest):
            continue
        if info.get("os") == "unknown" or info.get("os") != platform.os or info.get("architecture") != platform.architecture:
            continue
        if platform.variant and info.get("variant", "") != platform.variant:
            continue
        return digest
    raise RegistryError("the tag has no image for this platform")


class RegistryClient:
    """One run's worth of registry calls: remembers tokens, and the deadline of the run."""

    def __init__(
        self,
        credentials: dict[str, str] | None = None,
        deadline: float | None = None,
        step_timeout: float = 15.0,
    ) -> None:
        # registry host -> "user:password"
        self._credentials = credentials or {}
        self._deadline = deadline
        self._step_timeout = step_timeout
        self._tokens: dict[tuple[str, str], str] = {}  # (registry, scope) -> bearer token
        self._digests: dict[tuple[str, str, str], str] = {}  # (registry, repository, tag) -> digest, for this client's run

    # public

    def manifest_digest(self, ref: ImageRef, reference: str | None = None) -> str:
        """The digest the registry gives for the tag (a HEAD with Accept for index, list, OCI and Docker manifests).
        Asked once per tag for the client's life, one run: several images on the same tag cost one HEAD."""
        target = reference or ref.tag or "latest"
        key = (ref.registry, ref.repository, target)
        if key not in self._digests:
            self._digests[key] = self._head_digest(ref, target)
        return self._digests[key]

    def _head_digest(self, ref: ImageRef, target: str) -> str:
        path = f"/v2/{ref.repository}/manifests/{quote(target, safe=':')}"
        status, headers, _ = self._request("HEAD", ref, path, MANIFEST_ACCEPT)
        if status == 404:
            raise RegistryError("tag not found in the registry")
        if status != 200:
            raise RegistryError(f"registry answered HTTP {status}")
        digest = headers.get("docker-content-digest", "")
        if not _DIGEST.match(digest):
            raise RegistryError("registry gave no digest")
        return digest

    def layer_ids(self, ref: ImageRef, platform: Platform) -> tuple[str, ...]:
        """The layers (`rootfs.diff_ids` of the image config) of the tag for `platform`, or of the pinned digest. The
        tag's digest comes from a HEAD; the manifest GETs and the config blob GET happen once per digest."""
        reference = ref.digest or self.manifest_digest(ref)
        key = (ref.registry, ref.repository, reference, platform)
        with _LAYER_LOCK:
            if key in _LAYER_CACHE:
                return _LAYER_CACHE[key]
        base = f"/v2/{ref.repository}"
        manifest = self._get_json(ref, f"{base}/manifests/{reference}", MANIFEST_ACCEPT, MAX_BODY)
        if "manifests" in manifest:
            manifest = self._get_json(ref, f"{base}/manifests/{_pick_platform(manifest, platform)}", MANIFEST_ACCEPT, MAX_BODY)
        config = manifest.get("config")
        config_digest = config.get("digest") if isinstance(config, dict) else None
        if not isinstance(config_digest, str) or not _DIGEST.match(config_digest):
            raise RegistryError("the manifest names no image config")
        rootfs = self._get_json(ref, f"{base}/blobs/{config_digest}", "application/json", MAX_CONFIG, follow_redirect=True).get("rootfs")
        diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
        if not isinstance(diff_ids, list) or not all(isinstance(d, str) for d in diff_ids):
            raise RegistryError("the image config has no layer list")
        layers = tuple(diff_ids)
        with _LAYER_LOCK:
            _LAYER_CACHE[key] = layers
            while len(_LAYER_CACHE) > MAX_CACHED_LAYERS:
                del _LAYER_CACHE[next(iter(_LAYER_CACHE))]  # the oldest
        return layers

    def _get_json(self, ref: ImageRef, path: str, accept: str, limit: int, follow_redirect: bool = False) -> dict[str, object]:
        status, headers, body = self._request("GET", ref, path, accept, need_body=True, limit=limit)
        if follow_redirect and status in _REDIRECTS:
            # Docker Hub and ghcr.io send blobs from a CDN. One hop, and the registry's token stays with the registry.
            host, target = redirect_target(ref.registry, path, headers.get("location", ""))
            status, headers, body = self._once("GET", host, target, {"Accept": accept}, True, limit)
            if status in _REDIRECTS:
                raise RegistryError("the registry redirected twice")
        if status != 200:
            raise RegistryError(f"registry answered HTTP {status} for an image's layers")
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise RegistryError("the registry's answer is not JSON") from exc
        if not isinstance(data, dict):
            raise RegistryError("the registry's answer is not a JSON object")
        return data

    def list_tags(self, ref: ImageRef) -> tuple[list[str], bool]:
        """(tags, truncated): follows the `Link: rel="next"` pages, at most MAX_TAG_PAGES of them."""
        tags: list[str] = []
        path = f"/v2/{ref.repository}/tags/list?" + urlencode({"n": PAGE_SIZE})
        for page in range(MAX_TAG_PAGES):
            status, headers, body = self._request("GET", ref, path, "application/json", need_body=True)
            if status != 200:
                raise RegistryError(f"registry answered HTTP {status} for the tag list")
            try:
                found = json.loads(body.decode("utf-8")).get("tags") or []
            except (ValueError, AttributeError) as exc:
                raise RegistryError("the registry's tag list is not JSON") from exc
            tags.extend(t for t in found if isinstance(t, str))
            match = _LINK_NEXT.search(headers.get("link", ""))
            if not match:
                return tags, False
            nxt = urlsplit(urljoin(f"https://{ref.registry}{path}", match.group(1)))
            if nxt.netloc and nxt.netloc != ref.registry:
                raise RegistryError("the tag list continues on another host")
            path = nxt.path + (f"?{nxt.query}" if nxt.query else "")
            if page == MAX_TAG_PAGES - 1:
                return tags, True
        return tags, True  # pragma: no cover - the loop always returns

    # requests

    def _timeout(self) -> float:
        if self._deadline is None:
            return self._step_timeout
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RegistryError("timed out")
        return min(self._step_timeout, remaining)

    def _connect(self, host: str, timeout: float) -> http.client.HTTPConnection:
        if _is_local(host):
            return http.client.HTTPConnection(host, timeout=timeout)
        return http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())

    def _once(
        self,
        method: str,
        host: str,
        path: str,
        headers: dict[str, str],
        need_body: bool,
        limit: int = MAX_BODY,
    ) -> tuple[int, dict[str, str], bytes]:
        timeout = self._timeout()
        conn = self._connect(host, timeout)
        try:
            conn.request(method, path, headers={"User-Agent": "anystatus-checker", **headers})
            response = conn.getresponse()
            body = _read(response, time.monotonic() + timeout, limit) if need_body or response.status != 200 else b""
            return response.status, {k.lower(): v for k, v in response.getheaders()}, body
        except RegistryError:
            raise
        except ssl.SSLError as exc:
            raise RegistryError("TLS error", detail=f"{type(exc).__name__}: {exc}") from exc
        except http.client.HTTPException as exc:
            raise RegistryError("invalid answer from the registry", detail=type(exc).__name__) from exc
        except OSError as exc:
            raise RegistryError(describe_exception(exc), detail=f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()

    def _request(
        self, method: str, ref: ImageRef, path: str, accept: str, need_body: bool = False, limit: int = MAX_BODY
    ) -> tuple[int, dict[str, str], bytes]:
        host = ref.registry
        scope = f"repository:{ref.repository}:pull"
        headers = {"Accept": accept}
        token = self._tokens.get((host, scope))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, response_headers, body = self._once(method, host, path, headers, need_body, limit)
        if status != 401:
            return status, response_headers, body
        challenge = response_headers.get("www-authenticate", "")
        scheme = challenge.split(" ", 1)[0].lower()
        params = dict(_CHALLENGE_PARAM.findall(challenge))
        if scheme == "bearer" and "realm" in params:
            token = self._fetch_token(host, params, scope)
            self._tokens[(host, scope)] = token
            headers["Authorization"] = f"Bearer {token}"
        elif scheme == "basic" and host in self._credentials:
            headers["Authorization"] = "Basic " + base64.b64encode(self._credentials[host].encode()).decode("ascii")
        else:
            raise RegistryError("registry requires credentials (see registry_auth_env)")
        status, response_headers, body = self._once(method, host, path, headers, need_body, limit)
        if status in (401, 403):
            raise RegistryError("registry refused the credentials")
        return status, response_headers, body

    def _fetch_token(self, host: str, params: dict[str, str], scope: str) -> str:
        realm = urlsplit(params["realm"])
        if realm.scheme not in ("https", "http") or not realm.netloc:
            raise RegistryError("the registry's token address is not valid")
        if realm.scheme == "http" and not _is_local(realm.netloc):
            raise RegistryError("the registry's token address is not https")
        query = {"service": params["service"]} if "service" in params else {}
        query["scope"] = params.get("scope", scope)
        headers = {"Accept": "application/json"}
        # Credentials only go to the registry itself (or Docker Hub's own token service): a registry can't make the
        # checker send them elsewhere.
        trusted = realm.netloc == host or (host == HUB_REGISTRY and realm.netloc == HUB_AUTH_HOST)
        secret = self._credentials.get(host)
        if secret and trusted:
            headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode("ascii")
        path = realm.path + "?" + urlencode(query)
        status, _, body = self._once("GET", realm.netloc, path, headers, need_body=True)
        if status in (401, 403):
            raise RegistryError("registry refused the credentials" if secret else "registry requires credentials")
        if status != 200:
            raise RegistryError(f"token service answered HTTP {status}")
        try:
            data = json.loads(body.decode("utf-8"))
            token = data.get("token") or data.get("access_token")
        except (ValueError, AttributeError) as exc:
            raise RegistryError("the token service's answer is not JSON") from exc
        if not isinstance(token, str) or not token:
            raise RegistryError("the token service gave no token")
        return token


def _read(response: http.client.HTTPResponse, deadline: float, limit: int = MAX_BODY) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError
        chunk = response.read(65536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise RegistryError("the registry's answer is too large")
        chunks.append(chunk)
