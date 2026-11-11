"""A small container registry client (the v2 API) for `docker_updates`: image names, manifest digests, tag lists.

Standard library only. Reads only: HEAD for manifests, GET for tags and tokens. Redirects are not followed. The
usual Bearer challenge flow: a 401 carries `WWW-Authenticate: Bearer realm=..., service=..., scope=...`, the token
comes from the realm (anonymously, or with the configured credentials), and the request is repeated once with it.

Nothing secret (token, credentials, URLs) ever goes into an error text.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import ssl
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
MAX_BODY = 4_000_000
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
    registry: (
        str  # the host (with a port if there is one) that serves the API; Docker Hub is registry-1.docker.io
    )
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
    if (
        tag == ""
        or not path
        or not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*", path)
    ):
        raise ValueError("not an image name")
    if registry in _HUB_ALIASES:
        registry = HUB_REGISTRY
    if registry == HUB_REGISTRY and "/" not in path:
        path = f"library/{path}"
    if tag is None and digest is None:
        tag = "latest"
    return ImageRef(registry, path, tag, digest)


def _is_local(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name == "localhost" or name.startswith("127.") or name == "[::1]"


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

    # public

    def manifest_digest(self, ref: ImageRef, reference: str | None = None) -> str:
        """The digest the registry gives for the tag (a HEAD with Accept for index, list, OCI and Docker manifests)."""
        target = reference or ref.tag or "latest"
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
    ) -> tuple[int, dict[str, str], bytes]:
        timeout = self._timeout()
        conn = self._connect(host, timeout)
        try:
            conn.request(method, path, headers={"User-Agent": "anystatus-checker", **headers})
            response = conn.getresponse()
            body = _read(response, time.monotonic() + timeout) if need_body or response.status != 200 else b""
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
        self, method: str, ref: ImageRef, path: str, accept: str, need_body: bool = False
    ) -> tuple[int, dict[str, str], bytes]:
        host = ref.registry
        scope = f"repository:{ref.repository}:pull"
        headers = {"Accept": accept}
        token = self._tokens.get((host, scope))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, response_headers, body = self._once(method, host, path, headers, need_body)
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
            headers["Authorization"] = "Basic " + base64.b64encode(self._credentials[host].encode()).decode(
                "ascii"
            )
        else:
            raise RegistryError("registry requires credentials (see registry_auth_env)")
        status, response_headers, body = self._once(method, host, path, headers, need_body)
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
            raise RegistryError(
                "registry refused the credentials" if secret else "registry requires credentials"
            )
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


def _read(response: http.client.HTTPResponse, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError
        chunk = response.read(65536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_BODY:
            raise RegistryError("the registry's answer is too large")
        chunks.append(chunk)
