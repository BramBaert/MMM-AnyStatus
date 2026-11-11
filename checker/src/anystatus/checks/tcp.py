"""`tcp`: can a connection to host:port be made, and how fast? It proves something listens, nothing more."""

from __future__ import annotations

import socket
import time

from anystatus.checks.base import Check, register
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.table import Table


@register
class Tcp(Check):
    type_name = "tcp"
    placeholders = frozenset({"host", "port"})

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    @classmethod
    def from_config(cls, table: Table) -> Tcp:
        host = table.req_str("host")
        if not table.has("port"):
            table.error("missing 'port'")
        return cls(host, table.integer("port", 0, minimum=1, maximum=65535))

    def run(self, timeout: float) -> Observation | Reading:
        started = time.perf_counter()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except socket.gaierror as exc:
            # The name does not resolve: the check can't tell whether the port is open.
            raise CheckFailure("host name not found", detail=str(exc)) from exc
        except ConnectionRefusedError:
            return Observation(level="crit", summary=f"port {self.port} refused")
        except TimeoutError:
            return Observation(level="crit", summary=f"port {self.port}: no answer within {timeout:g} s")
        except OSError as exc:
            # Network or host unreachable and the like: nothing answered on that port.
            raise CheckFailure("cannot connect", detail=f"{type(exc).__name__}: {exc}") from exc
        elapsed = (time.perf_counter() - started) * 1000
        sock.close()
        ms = round(elapsed, 1)
        return Reading(
            value=ms,
            unit="ms",
            text=f"port {self.port} open, {ms:g} ms",
            fields={"host": self.host, "port": str(self.port)},
        )
