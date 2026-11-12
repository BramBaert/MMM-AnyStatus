"""`exec`: run a command (without a shell) that follows the Nagios plugin conventions.

Exit 0, 1, 2, 3 are ok, warn, crit, unknown; any other exit code is a failure. The first line of the output, up to
`|`, is the summary; the first performance value after the `|` (`label=value[UOM];warn;crit;...`) is the value and
unit. The plugin's own thresholds win, so `warn` and `crit` are not accepted.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from anystatus.checks.base import Check, register
from anystatus.errors import mask
from anystatus.model import CheckFailure, Observation
from anystatus.table import Table

log = logging.getLogger("anystatus")

MAX_OUTPUT = 64 * 1024  # bytes read from each of stdout and stderr; the rest is read and thrown away
_LEVELS = {0: "ok", 1: "warn", 2: "crit", 3: "unknown"}
# label=value[UOM];...  The label is bare or 'quoted'; the value is a number or U (unknown); the UOM is optional.
_PERFDATA = re.compile(r"\s*(?:'(?:[^']|'')*'|[^=\s']+)=([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|U)([^\d;\s'][^;\s]*)?")
_DRAIN_GRACE = 2.0  # seconds to wait for the output after the process has ended (a child may keep the pipe open)


def parse_plugin_output(text: str) -> tuple[str, float | None, str | None]:
    """(summary, value, unit) from a plugin's output. The summary is the first line up to `|`."""
    lines = text.splitlines()
    first = lines[0] if lines else ""
    summary, bar, perf = first.partition("|")
    value: float | None = None
    unit: str | None = None
    if bar:
        m = _PERFDATA.match(perf)
        if m and m.group(1) != "U":
            number = float(m.group(1))
            if number == number and abs(number) != float("inf"):
                value, unit = number, m.group(2) or None
    return summary.strip(), value, unit


class _Drain:
    """Reads a pipe on its own thread, keeps the first `limit` bytes, and discards the rest so the process never blocks."""

    def __init__(self, stream: object, limit: int) -> None:
        self.data = bytearray()
        self.limit = limit
        self._stream = stream
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            while chunk := self._stream.read1(65536):  # type: ignore[attr-defined]
                room = self.limit - len(self.data)
                if room > 0:
                    self.data += chunk[:room]

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill the process and, where the OS allows, everything it started."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # it leads its own session (start_new_session)
    with contextlib.suppress(OSError):
        proc.kill()


@dataclass(frozen=True)
class ProcessResult:
    code: int
    stdout: str
    stderr: str  # masked and stripped; for the debug log only, never for a summary
    truncated: bool  # the process wrote more to stdout than `limit` bytes


def run_process(command: Sequence[str], env: dict[str, str], timeout: float, limit: int, secrets: Sequence[str] = ()) -> ProcessResult:
    """Run a command without a shell and wait for it. A timeout kills it (and what it started) and raises
    CheckFailure; so does a command that can't be started. `limit` bytes of each output stream are kept."""
    flags = 0
    if sys.platform == "win32":  # an if statement, not a conditional expression: mypy only narrows on this form
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(  # no shell: the command is a list
            list(command),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
    except FileNotFoundError as exc:
        raise CheckFailure("command not found", detail=str(exc)) from exc
    except OSError as exc:
        raise CheckFailure("cannot run the command", detail=f"{type(exc).__name__}: {exc}") from exc

    assert proc.stdout is not None and proc.stderr is not None
    out, err = _Drain(proc.stdout, limit), _Drain(proc.stderr, limit)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        proc.wait()
        out.thread.join(_DRAIN_GRACE)
        err.thread.join(_DRAIN_GRACE)
        log.debug("exec: stderr after the timeout: %s", mask(err.text(), secrets))
        raise CheckFailure(f"timed out after {timeout:g} s") from None
    out.thread.join(_DRAIN_GRACE)
    err.thread.join(_DRAIN_GRACE)
    return ProcessResult(code, out.text(), mask(err.text(), secrets).strip(), len(out.data) >= limit)


@register
class Exec(Check):
    type_name = "exec"

    def __init__(self, command: list[str], env_names: list[str]) -> None:
        self.command = command
        self.env_names = env_names

    @classmethod
    def from_config(cls, table: Table) -> Exec:
        for key in ("warn", "crit", "worse"):
            if table.has(key):
                table.error(f"exec takes the plugin's own thresholds: '{key}' is not used")
        command: list[str] = []
        if table.has("command"):
            given = table.raw("command")
            if isinstance(given, str):
                table.error(
                    '\'command\' must be a list such as ["/usr/lib/nagios/plugins/check_x", "-w", "5"], not a string: it is run without a shell'
                )
            elif not isinstance(given, list) or not given or not all(isinstance(a, str) for a in given):
                table.error("'command' must be a non-empty list of text values")
            elif not given[0]:
                table.error("'command' starts with an empty program name")
            else:
                command = list(given)
        else:
            table.error("missing 'command'")
        env_names = table.str_list("env") or []
        return cls(command, env_names)

    def _environment(self) -> dict[str, str]:
        """Only the listed variables, and only those that are set: nothing else, PATH included."""
        return {name: os.environ[name] for name in self.env_names if name in os.environ}

    def run(self, timeout: float) -> Observation:
        env = self._environment()
        secrets = [v for v in env.values() if v]
        done = run_process(self.command, env, timeout, MAX_OUTPUT, secrets)
        code, stdout, stderr_text = done.code, done.stdout, done.stderr
        if stderr_text:
            log.debug("exec %s: stderr: %s", os.path.basename(self.command[0]), stderr_text)

        if code not in _LEVELS:
            how = f"killed by signal {-code}" if code < 0 else f"exit code {code}"
            raise CheckFailure(how, detail=f"{how}; stderr: {stderr_text}" if stderr_text else how)
        summary, value, unit = parse_plugin_output(stdout)
        level = _LEVELS[code]
        if not summary and level != "ok":
            summary = f"no output (exit code {code})"
        return Observation(level=level, summary=summary or None, value=value, unit=unit)
