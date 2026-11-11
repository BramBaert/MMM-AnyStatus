"""A tiny, read-only Docker Engine API client for the docker_* check types. Standard library only, no Docker SDK.

- `docker = "unix:///var/run/docker.sock"` (HTTP over a unix socket) or `"tcp://host:2375"` (plain HTTP, e.g. a
  docker-socket-proxy). On Windows only tcp:// is possible (the checker reaches Docker through a socket proxy).
- **Only GET can be sent**: `DockerClient.get_json` is the whole public surface and it hard-codes the method.
- No API version in the path: the daemon answers unversioned paths in its own current API version. A fixed
  /v1.41 would be refused by newer engines (Docker 29 dropped API versions older than 1.44), negotiating needs
  GET /version (which a socket proxy refuses unless VERSION=1), and every field read here is older than API 1.24.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from anystatus.errors import describe_exception
from anystatus.model import CheckFailure
from anystatus.table import Table

DEFAULT_DOCKER = "unix:///var/run/docker.sock"
MAX_BODY = 8_000_000  # bytes of one answer; more is a failure


@dataclass(frozen=True)
class Endpoint:
    unix_path: str | None = None
    host: str | None = None
    port: int | None = None


def is_windows() -> bool:
    return sys.platform == "win32"


def parse_endpoint(text: str) -> Endpoint:
    """`unix:///path` or `tcp://host[:port]`. Raises ValueError with a message for the config."""
    parts = urlsplit(text)
    if parts.scheme == "unix":
        if not parts.path or parts.netloc:
            raise ValueError("a unix address looks like unix:///var/run/docker.sock")
        if is_windows():
            raise ValueError(
                "unix sockets are not available on Windows: reach Docker through a socket proxy, "
                'for example docker = "tcp://localhost:2375"'
            )
        return Endpoint(unix_path=parts.path)
    if parts.scheme == "tcp":
        try:
            port = parts.port
        except ValueError:
            raise ValueError("the port is not a number") from None
        if not parts.hostname or parts.username or parts.password or parts.path not in ("", "/"):
            raise ValueError("a tcp address looks like tcp://socket-proxy:2375")
        return Endpoint(host=parts.hostname, port=port or 2375)
    raise ValueError('\'docker\' must be "unix:///path" or "tcp://host:port"')


def docker_endpoint(table: Table) -> Endpoint:
    """The `docker` key of a check, recording a problem on the table."""
    text = table.opt_str("docker", DEFAULT_DOCKER) or DEFAULT_DOCKER
    try:
        return parse_endpoint(text)
    except ValueError as exc:
        table.error(f"'docker': {exc}")
        return Endpoint(host="localhost", port=2375)


class NotFound(CheckFailure):
    """The Docker API answered 404 (a container or image that went away between two calls)."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined,unused-ignore]
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._unix_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class DockerClient:
    """Reads from the Docker API. Nothing but GET."""

    def __init__(self, endpoint: Endpoint) -> None:
        self._endpoint = endpoint

    def get_json(self, path: str, query: Mapping[str, str] | None = None, *, timeout: float) -> Any:
        target = path
        if query:
            target += "?" + "&".join(f"{quote(k)}={quote(v)}" for k, v in query.items())
        e = self._endpoint
        conn: http.client.HTTPConnection = (
            _UnixConnection(e.unix_path, timeout)
            if e.unix_path is not None
            else http.client.HTTPConnection(e.host or "localhost", e.port, timeout=timeout)
        )
        deadline = time.monotonic() + timeout
        try:
            conn.request("GET", target, headers={"Host": "docker", "Accept": "application/json"})
            response = conn.getresponse()
            body = _read(response, deadline)
        except CheckFailure:
            raise
        except http.client.HTTPException as exc:
            raise CheckFailure("invalid answer from Docker", detail=type(exc).__name__) from exc
        except OSError as exc:
            raise CheckFailure(
                f"Docker unreachable: {describe_exception(exc)}", detail=f"{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            conn.close()
        status = response.status
        if status == 404:
            raise NotFound("not found by the Docker API")
        if status in (401, 403):
            raise CheckFailure(f"Docker API refused the request (HTTP {status}): is that path allowed?")
        if status != 200:
            raise CheckFailure(f"Docker API answered HTTP {status}")
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise CheckFailure("the Docker API answer is not JSON") from exc


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
            raise CheckFailure("the Docker API answer is too large")
        chunks.append(chunk)


@dataclass(frozen=True)
class RunningImage:
    """An image that at least one running container uses."""

    reference: str  # as the container was created with ("nginx:1.27"), or "" when only the id is known
    image_id: str
    containers: tuple[str, ...]


def _looks_like_id(reference: str) -> bool:
    return reference.startswith("sha256:") or (
        len(reference) in (12, 64) and all(c in "0123456789abcdef" for c in reference)
    )


def running_images(client: DockerClient, timeout: float) -> list[RunningImage]:
    """The distinct images of running containers: (reference, image id) pairs, in a stable order."""
    listing = client.get_json("/containers/json", timeout=timeout)
    if not isinstance(listing, list):
        raise CheckFailure("unexpected answer from the Docker API")
    found: dict[tuple[str, str], list[str]] = {}
    for item in listing:
        if not isinstance(item, dict):
            continue
        reference = item.get("Image")
        image_id = item.get("ImageID")
        if not isinstance(reference, str) or not isinstance(image_id, str):
            continue
        names = item.get("Names")
        name = names[0].lstrip("/") if isinstance(names, list) and names else str(item.get("Id", ""))[:12]
        found.setdefault(("" if _looks_like_id(reference) else reference, image_id), []).append(name)
    return [RunningImage(ref, image_id, tuple(names)) for (ref, image_id), names in sorted(found.items())]


def image_info(client: DockerClient, image_id: str, timeout: float) -> tuple[list[str], list[str]]:
    """(RepoTags, RepoDigests) of a local image."""
    data = client.get_json(f"/images/{quote(image_id, safe=':')}/json", timeout=timeout)
    if not isinstance(data, dict):
        raise CheckFailure("unexpected answer from the Docker API")

    def strings(key: str) -> list[str]:
        value = data.get(key)
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    return strings("RepoTags"), strings("RepoDigests")
