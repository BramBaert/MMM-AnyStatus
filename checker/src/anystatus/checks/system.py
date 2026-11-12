"""`system`: temperature, load, memory and uptime of the machine, read from /proc and /sys. Linux only.

Elsewhere the check is a failure ("only on Linux"), not a config error, so a config written for a Pi still loads and
validates on a laptop. `root` is where /proc and /sys are looked for: tests point it at a fake tree.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from anystatus.checks.base import Check, Traits, register
from anystatus.model import CheckFailure, Reading
from anystatus.table import Table
from anystatus.timeutil import format_duration

MEASURES = ("temperature", "load", "memory", "uptime")
_ZONE = re.compile(r"^[A-Za-z0-9_.-]+$")
GB = 1000**3


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


@register
class System(Check):
    type_name = "system"

    def __init__(self, measure: str, zone: str | None, root: Path = Path("/")) -> None:
        self.measure = measure
        self.zone = zone
        self.root = root

    @classmethod
    def traits(cls, table: Table) -> Traits:
        # Only uptime is a duration ("30d"); a check for a machine that rebooted would use worse = "below".
        return Traits("above", table.data.get("measure") == "uptime", frozenset({"zone"}), (None, None))

    @classmethod
    def from_config(cls, table: Table) -> System:
        measure = table.choice("measure", MEASURES, "temperature")
        zone = table.opt_str("zone")
        if zone is not None and (measure != "temperature" or not _ZONE.match(zone)):
            table.error('\'zone\' is only for measure = "temperature" and names a thermal zone such as "thermal_zone0"')
            zone = None
        return cls(measure, zone)

    def run(self, timeout: float) -> Reading:
        if not _is_linux():
            raise CheckFailure("system checks only work on Linux")
        if self.measure == "temperature":
            return self._temperature()
        if self.measure == "load":
            return self._load()
        if self.measure == "memory":
            return self._memory()
        return self._uptime()

    # reading

    def _read(self, relative: str, what: str) -> str:
        try:
            return (self.root / relative).read_text(encoding="ascii", errors="replace")
        except OSError as exc:
            raise CheckFailure(f"cannot read {what}", detail=f"{relative}: {type(exc).__name__}") from exc

    def _number(self, relative: str, what: str) -> float:
        text = self._read(relative, what).split()
        try:
            return float(text[0])
        except (ValueError, IndexError) as exc:
            raise CheckFailure(f"cannot read {what}", detail=f"{relative}: unexpected content") from exc

    # the measures

    def _temperature(self) -> Reading:
        base = self.root / "sys/class/thermal"
        try:
            names = sorted(p.name for p in base.iterdir() if p.name.startswith("thermal_zone"))
        except OSError:
            names = []
        if self.zone is not None:
            if self.zone not in names:
                raise CheckFailure(f"no thermal zone {self.zone}")
            names = [self.zone]
        best: tuple[float, str] | None = None
        for name in names:
            try:
                milli = float((base / name / "temp").read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                continue  # a zone that can't be read (some answer EINVAL) is skipped
            if best is None or milli > best[0]:
                best = (milli, name)
        if best is None:
            raise CheckFailure("no temperature sensor")
        celsius = round(best[0] / 1000, 1)
        return Reading(value=celsius, unit="°C", text=f"{celsius:g} °C ({best[1]})", fields={"zone": best[1]})

    def _load(self) -> Reading:
        load = self._number("proc/loadavg", "the load average")
        cpus = self._cpu_count()
        per_cpu = round(load / cpus, 2)
        return Reading(value=per_cpu, text=f"load {per_cpu:g} per CPU ({load:g} on {cpus})")

    def _cpu_count(self) -> int:
        try:
            text = (self.root / "proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
            found = len(re.findall(r"^processor\s*:", text, re.MULTILINE))
        except OSError:
            found = 0
        return found or os.cpu_count() or 1

    def _memory(self) -> Reading:
        info: dict[str, int] = {}
        for line in self._read("proc/meminfo", "the memory figures").splitlines():
            name, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                info[name] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1)
        total, available = info.get("MemTotal"), info.get("MemAvailable")
        if not total or available is None:
            raise CheckFailure("cannot read the memory figures", detail="MemTotal or MemAvailable is missing")
        used, size, scope = total - available, total, ""
        limited = self._cgroup_memory(total)
        if limited is not None:
            used, size, scope = limited[0], limited[1], ", container limit"
        percent = round(max(0.0, used) / size * 100, 1)
        return Reading(value=percent, unit="%", text=f"{percent:g} % of {size / GB:.1f} GB used{scope}")

    def _cgroup_memory(self, host_total: int) -> tuple[int, int] | None:
        """(used, limit) in bytes when a cgroup v2 memory limit below the machine's memory applies, else None.
        Used is what the cgroup holds minus the file cache that can be dropped, as `docker stats` counts it."""
        try:
            limit_text = (self.root / "sys/fs/cgroup/memory.max").read_text(encoding="ascii").strip()
            if not limit_text.isdigit() or not 0 < int(limit_text) < host_total:
                return None
            current = int((self.root / "sys/fs/cgroup/memory.current").read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None
        inactive = 0
        try:
            for line in (self.root / "sys/fs/cgroup/memory.stat").read_text(encoding="ascii").splitlines():
                key, _, number = line.partition(" ")
                if key == "inactive_file" and number.strip().isdigit():
                    inactive = int(number)
        except OSError:
            pass
        return current - inactive, int(limit_text)

    def _uptime(self) -> Reading:
        seconds = self._number("proc/uptime", "the uptime")
        shown, unit = format_duration(seconds)
        return Reading(value=seconds, seconds=True, text=f"up {shown:.1f} {unit}")
