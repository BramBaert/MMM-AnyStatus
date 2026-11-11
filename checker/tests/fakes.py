"""Fakes for the docker_* and image_vulns tests: a Docker API, a container registry (both plain HTTP servers in a
thread on 127.0.0.1) and a fake `trivy` program. No real Docker, registry or Trivy is needed."""

from __future__ import annotations

import copy
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).parent / "fixtures"


class _Server:
    handler: type[BaseHTTPRequestHandler]

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []  # (method, path and query), as received
        fake = self

        class Handler(self.handler):  # type: ignore[name-defined]
            owner = fake

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _send(
    handler: BaseHTTPRequestHandler, status: int, body: bytes = b"", headers: dict[str, str] | None = None
) -> None:
    handler.send_response(status)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


# ---------------------------------------------------------------------------------------------------- Docker API


class _DockerHandler(BaseHTTPRequestHandler):
    owner: FakeDocker

    def _handle(self) -> None:
        o = self.owner
        o.requests.append((self.command, self.path))
        if self.command != "GET":
            _send(self, 405, b'{"message":"method not allowed"}')
            return
        if o.refuse is not None:
            _send(self, o.refuse, b'{"message":"refused"}')
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        segments = parts.path.strip("/").split("/")
        if parts.path == "/containers/json":
            listing = o.list_containers(
                all_=query.get("all") == ["true"], filters=json.loads(query.get("filters", ["{}"])[0])
            )
            _send(self, 200, json.dumps(listing).encode(), {"Content-Type": "application/json"})
        elif len(segments) == 3 and segments[0] == "containers" and segments[2] == "json":
            info = o.inspect.get(segments[1])
            if info is None:
                _send(self, 404, b'{"message":"no such container"}')
            else:
                _send(self, 200, json.dumps(info).encode(), {"Content-Type": "application/json"})
        elif len(segments) == 3 and segments[0] == "images" and segments[2] == "json":
            image = o.images.get(segments[1])
            if image is None:
                _send(self, 404, b'{"message":"no such image"}')
            else:
                _send(self, 200, json.dumps(image).encode(), {"Content-Type": "application/json"})
        else:
            _send(self, 404, b'{"message":"page not found"}')

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _handle


class FakeDocker(_Server):
    """Serves /containers/json, /containers/{id}/json and /images/{id}/json from state the test can change.

    Starts from the captured fixture (a Docker Desktop's answers, trimmed and anonymised) unless `empty`."""

    handler = _DockerHandler

    def __init__(self, empty: bool = False) -> None:
        self.list: list[dict[str, Any]] = []
        self.inspect: dict[str, dict[str, Any]] = {}
        self.images: dict[str, dict[str, Any]] = {}
        self.refuse: int | None = None  # answer every request with this status (a socket proxy that says no)
        if not empty:
            data = json.loads((FIXTURES / "docker_dev_stack.json").read_text(encoding="utf-8"))
            self.list, self.inspect, self.images = data["list"], data["inspect"], data["images"]
        super().__init__()

    @property
    def url(self) -> str:
        return f"tcp://127.0.0.1:{self.port}"

    def list_containers(self, all_: bool, filters: dict[str, list[str]]) -> list[dict[str, Any]]:
        out = []
        for item in self.list:
            if not all_ and item["State"] != "running":
                continue
            labels = item.get("Labels") or {}
            if not all(
                (
                    want.split("=")[0] in labels
                    and (("=" not in want) or labels[want.split("=")[0]] == want.split("=", 1)[1])
                )
                for want in filters.get("label", [])
            ):
                continue
            out.append(item)
        return out

    def by_name(self, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        for item in self.list:
            if item["Names"][0] == "/" + name:
                return item, self.inspect[item["Id"]]
        raise KeyError(name)

    def set_state(
        self,
        name: str,
        status: str | None = None,
        health: str | None = None,
        restarts: int | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        item, info = self.by_name(name)
        if status is not None:
            item["State"], info["State"]["Status"] = status, status
            info["State"]["Running"] = status == "running"
        if health is not None:
            info["State"]["Health"] = {"Status": health}
        if restarts is not None:
            info["RestartCount"] = restarts
        if labels is not None:
            item["Labels"] = labels

    def recreate(self, name: str, new_id: str) -> None:
        """The container is replaced by a new one with the same name (its RestartCount starts again)."""
        item, info = self.by_name(name)
        del self.inspect[item["Id"]]
        item["Id"] = info["Id"] = new_id
        info["RestartCount"] = 0
        self.inspect[new_id] = info

    def add_container(
        self,
        name: str,
        image: str,
        image_id: str,
        repo_digests: list[str] | None,
        repo_tags: list[str] | None = None,
    ) -> None:
        container_id = (name.encode().hex() * 8)[:64]
        self.list.append(
            {
                "Id": container_id,
                "Names": ["/" + name],
                "Image": image,
                "ImageID": image_id,
                "State": "running",
                "Status": "Up 1 hour",
                "Labels": {},
            }
        )
        self.inspect[container_id] = {
            "Id": container_id,
            "Name": "/" + name,
            "Image": image_id,
            "RestartCount": 0,
            "State": {"Status": "running", "Running": True},
            "Config": {"Image": image},
        }
        self.images[image_id] = {
            "Id": image_id,
            "RepoTags": repo_tags if repo_tags is not None else [image],
            "RepoDigests": repo_digests or [],
        }

    def only(self, *names: str) -> None:
        """Keep only these containers (and their images)."""
        keep = {"/" + n for n in names}
        self.list = [i for i in self.list if i["Names"][0] in keep]
        ids = {i["Id"] for i in self.list}
        self.inspect = {k: v for k, v in self.inspect.items() if k in ids}
        images = {i["ImageID"] for i in self.list}
        self.images = {k: v for k, v in self.images.items() if k in images}

    def methods(self) -> set[str]:
        return {m for m, _ in self.requests}


# ---------------------------------------------------------------------------------------------------- registry


def digest(seed: str) -> str:
    """A fake sha256 digest that is always the same for the same seed."""
    import hashlib

    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)


class _RegistryHandler(BaseHTTPRequestHandler):
    owner: FakeRegistry

    def _handle(self) -> None:
        o = self.owner
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        o.requests.append((self.command, self.path))
        auth = self.headers.get("Authorization", "")
        o.seen.append(
            {
                "method": self.command,
                "path": parts.path,
                "authorization": auth,
                "accept": self.headers.get("Accept", ""),
            }
        )

        if parts.path == "/token":
            o.token_requests.append({"query": {k: v[0] for k, v in query.items()}, "authorization": auth})
            if o.token_needs_basic and not auth.startswith("Basic "):
                _send(self, 401, b'{"errors":[{"code":"UNAUTHORIZED"}]}')
            elif o.token_status != 200:
                _send(self, o.token_status, b"{}")
            else:
                _send(self, 200, json.dumps({"token": o.token}).encode())
            return
        if not parts.path.startswith("/v2/"):
            _send(self, 404)
            return
        repo_path = parts.path[len("/v2/") :]
        if o.status is not None:
            _send(self, o.status)
            return
        if o.require_auth and auth != f"Bearer {o.token}":
            challenge = (
                f'Bearer realm="{o.realm or f"http://127.0.0.1:{o.port}/token"}",service="fake-registry"'
            )
            repo = repo_path.split("/manifests/")[0].split("/tags/")[0]
            _send(self, 401, b"{}", {"WWW-Authenticate": f'{challenge},scope="repository:{repo}:pull"'})
            return
        if "/manifests/" in repo_path:
            repo, _, tag = repo_path.partition("/manifests/")
            found = o.manifests.get((repo, tag))
            if found is None:
                _send(self, 404)
                return
            accept = self.headers.get("Accept", "")
            wants_index = all(t in accept for t in INDEX_TYPES)
            # A multi-arch tag answers an index to a client that accepts one, and just one platform's manifest if not.
            value = found["index"] if wants_index and found.get("index") else found["manifest"]
            _send(
                self,
                200,
                b"" if self.command == "HEAD" else b"{}",
                {"Docker-Content-Digest": value, "Content-Type": "application/json"},
            )
            return
        if repo_path.endswith("/tags/list"):
            repo = repo_path[: -len("/tags/list")]
            tags = o.tags.get(repo)
            if tags is None:
                _send(self, 404)
                return
            start = int(query.get("last", ["0"])[0]) if query.get("last") else 0
            chunk = tags[start : start + o.page_size] if o.page_size else tags
            headers = {}
            if o.page_size and start + o.page_size < len(tags):
                headers["Link"] = (
                    f'</v2/{repo}/tags/list?last={start + o.page_size}&n={o.page_size}>; rel="next"'
                )
            _send(self, 200, json.dumps({"name": repo, "tags": chunk}).encode(), headers)
            return
        _send(self, 404)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle


class FakeRegistry(_Server):
    handler = _RegistryHandler

    def __init__(self) -> None:
        self.manifests: dict[
            tuple[str, str], dict[str, str]
        ] = {}  # (repo, tag) -> {"index": ..., "manifest": ...}
        self.tags: dict[str, list[str]] = {}
        self.page_size = 0  # 0: one page
        self.status: int | None = None  # answer every /v2/ request with this status (a registry that is down)
        self.require_auth = False
        self.token = "tok-3f9a1c"
        self.realm: str | None = None
        self.token_needs_basic = False
        self.token_status = 200
        self.token_requests: list[dict[str, Any]] = []
        self.seen: list[dict[str, str]] = []
        super().__init__()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def add(self, repo: str, tag: str, index: str | None = None, manifest: str | None = None) -> None:
        self.manifests[(repo, tag)] = {
            "index": index or "",
            "manifest": manifest or index or digest(f"{repo}:{tag}"),
        }


# ---------------------------------------------------------------------------------------------------- Trivy


def write_fake_trivy(directory: Path, outputs: dict[str, Any], default: Any | None = None) -> list[str]:
    """A `trivy` stand-in: a Python script that prints the canned JSON for the image it is asked to scan (the last
    argument) and records its arguments. Returns the command as a list (`trivy = [...]`). `outputs` maps an image
    name to a JSON value, to the string "fail" (exit 1), or to "hang" (sleeps)."""
    script = directory / "fake_trivy.py"
    data = directory / "fake_trivy.json"
    data.write_text(json.dumps({"outputs": outputs, "default": default}), encoding="utf-8")
    script.write_text(
        "import json, sys, time, pathlib\n"
        f"cfg = json.loads(pathlib.Path({str(data)!r}).read_text(encoding='utf-8'))\n"
        f"with open({str(directory / 'calls.jsonl')!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "image = sys.argv[-1]\n"
        "out = cfg['outputs'].get(image, cfg['default'])\n"
        "if out == 'fail':\n"
        "    sys.stderr.write('FATAL something went wrong\\n'); sys.exit(1)\n"
        "if out == 'hang':\n"
        "    time.sleep(60)\n"
        "if out is None:\n"
        "    sys.stderr.write('no canned output for ' + image + '\\n'); sys.exit(1)\n"
        "print(json.dumps(out))\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def trivy_calls(directory: Path) -> list[list[str]]:
    path = directory / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def trivy_json(vulns: list[tuple[str, str, str | None]]) -> dict[str, Any]:
    """Trivy's JSON for (id, severity, fixed version or None) vulnerabilities."""
    return {
        "SchemaVersion": 2,
        "Results": [
            {
                "Target": "image (alpine 3.20)",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": vuln_id,
                        "PkgName": f"pkg-{vuln_id[-4:]}",
                        "InstalledVersion": "1.0",
                        **({"FixedVersion": fixed} if fixed else {}),
                        "Severity": severity,
                    }
                    for vuln_id, severity, fixed in vulns
                ],
            }
        ],
    }


def fixture(name: str) -> Any:
    return copy.deepcopy(json.loads((FIXTURES / name).read_text(encoding="utf-8")))
