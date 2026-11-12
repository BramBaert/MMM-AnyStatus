"""`ping`: runs the system `ping` command (no raw sockets, no root) and parses its output.

Understood: Linux iputils, BusyBox, and Windows (English; the numbers and the `ms` of the reply lines and the
"label = N, label = N, label = N (X%" statistics line are read without relying on the words, so Dutch, German and
French Windows work too). Anything else that can't be read is a failure (`unknown`), never a guess.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from anystatus.checks.base import Check, register
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.table import Table

# One reply: "time=0.045 ms" (Linux), "time=12ms" / "time<1ms" (Windows), "tijd=12 ms", "Zeit<1ms", "temps=12 ms".
_REPLY = re.compile(r"\b(?:time|tijd|zeit|temps|tiempo|tempo)\s*([=<])\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)
# "3 packets transmitted, 3 received, 0% packet loss" (iputils) / "..., 3 packets received, ..." (BusyBox)
_STATS_UNIX = re.compile(r"(\d+)\s+packets transmitted,\s*(\d+)(?:\s+packets)?\s+received", re.IGNORECASE)
# "Packets: Sent = 3, Received = 3, Lost = 0 (0% loss)" and its translations: three numbers, then "(N%"
_STATS_WINDOWS = re.compile(r"^[^\d\n=]+=\s*(\d+),[^\d\n=]+=\s*(\d+),[^\d\n=]+=\s*(\d+)\s*\(\d+\s*%", re.MULTILINE)
_HOST = re.compile(r"^[A-Za-z0-9._:%-]+$")


@dataclass(frozen=True)
class PingOutput:
    sent: int | None  # from the statistics line, when there was one
    times: list[float]  # the round trip of every reply, in ms
    recognised: bool  # the output looked like ping's


def parse_ping_output(output: str) -> PingOutput:
    times = [float(m.group(2).replace(",", ".")) for m in _REPLY.finditer(output)]
    sent: int | None = None
    recognised = bool(times)
    unix = _STATS_UNIX.search(output)
    if unix:
        sent, recognised = int(unix.group(1)), True
    else:
        windows = _STATS_WINDOWS.search(output)
        if windows:
            sent, recognised = int(windows.group(1)), True
    return PingOutput(sent, times, recognised)


@register
class Ping(Check):
    type_name = "ping"
    placeholders = frozenset({"host", "answered", "sent"})

    def __init__(self, host: str, count: int) -> None:
        self.host = host
        self.count = count

    @classmethod
    def from_config(cls, table: Table) -> Ping:
        host = table.req_str("host")
        if host and not _HOST.match(host):
            table.error(f"'host' is not a valid host name or address: {host!r}")
        if host.startswith("-"):
            table.error("'host' must not start with '-'")
        count = table.integer("count", 3, minimum=1, maximum=20)
        return cls(host, count)

    def command(self, timeout: float) -> list[str]:
        wait = max(1, int(timeout / self.count) - 1)
        if sys.platform == "win32":
            return ["ping", "-n", str(self.count), "-w", str(wait * 1000), self.host]
        deadline = max(1, int(timeout) - 1)
        return ["ping", "-c", str(self.count), "-W", str(wait), "-w", str(deadline), self.host]

    def run(self, timeout: float) -> Observation | Reading:
        try:
            proc = subprocess.run(self.command(timeout), capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise CheckFailure("ping command not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise CheckFailure("ping did not finish in time") from exc
        # Windows prints in the OEM code page; only the ASCII parts matter, and latin-1 never fails.
        text = proc.stdout.decode("latin-1") + proc.stderr.decode("latin-1")
        return self.interpret(text, proc.returncode)

    def interpret(self, text: str, returncode: int) -> Observation | Reading:
        parsed = parse_ping_output(text)
        if not parsed.recognised:
            raise CheckFailure(
                "ping failed" if returncode else "could not read ping output",
                detail=f"exit code {returncode}, output: {text[:500]}",
            )
        sent = max(parsed.sent or self.count, len(parsed.times))
        answered = len(parsed.times)
        fields: dict[str, Any] = {"host": self.host, "answered": str(answered), "sent": str(sent)}
        if answered == 0:
            return Observation(level="crit", summary="no answer")
        average = round(sum(parsed.times) / answered, 1)
        text_out = f"{average:g} ms" if answered == sent else f"{answered}/{sent} answered, {average:g} ms"
        return Reading(value=average, unit="ms", text=text_out, fields=fields)
