"""The HTTP side: GET /results (JSON) and GET / (plain text), answered from the snapshot. Answering never runs checks."""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from anystatus.document import Snapshot

log = logging.getLogger("anystatus")


def make_handler(snapshot: Snapshot) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "anystatus-checker"

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/results":
                self._send(200, "application/json", snapshot.json_bytes())
            elif path == "/":
                self._send(200, "text/plain; charset=utf-8", snapshot.overview_bytes())
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("http: " + format, *args)

    return Handler


class ResultsServer:
    def __init__(self, host: str, port: int, snapshot: Snapshot) -> None:
        self.httpd = ThreadingHTTPServer((host, port), make_handler(snapshot))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="http", daemon=True)

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
