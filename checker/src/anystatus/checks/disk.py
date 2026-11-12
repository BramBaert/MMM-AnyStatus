"""`disk`: how full is the filesystem behind a path?"""

from __future__ import annotations

import shutil

from anystatus.checks.base import Check, Traits, register
from anystatus.model import CheckFailure, Reading
from anystatus.table import Table

GB = 1000**3  # decimal, as drives are sold and `df -H` shows


@register
class Disk(Check):
    type_name = "disk"
    placeholders = frozenset({"free"})
    default_limits = (85.0, 95.0)  # for measure = "used"; "free" has none

    def __init__(self, path: str, measure: str) -> None:
        self.path = path
        self.measure = measure

    @classmethod
    def traits(cls, table: Table) -> Traits:
        if table.data.get("measure") == "free":
            return Traits("below", False, cls.placeholders, (None, None))
        return Traits("above", False, cls.placeholders, cls.default_limits)

    @classmethod
    def from_config(cls, table: Table) -> Disk:
        return cls(table.path("path"), table.choice("measure", ("used", "free"), "used"))

    def run(self, timeout: float) -> Reading:
        try:
            usage = shutil.disk_usage(self.path)
        except FileNotFoundError as exc:
            raise CheckFailure("path not found", detail=str(exc)) from exc
        except OSError as exc:
            raise CheckFailure("cannot read the disk usage", detail=f"{type(exc).__name__}: {exc}") from exc
        # Like `df`: the share of what a normal user can use, so a disk with reserved blocks reads 100 % when full.
        usable = usage.used + usage.free
        if usable <= 0:
            raise CheckFailure("the filesystem reports no size")
        free_gb = round(usage.free / GB, 1)
        fields = {"free": f"{free_gb:g}"}
        if self.measure == "free":
            return Reading(value=free_gb, unit="GB", text=f"{free_gb:g} GB free", fields=fields)
        percent = round(usage.used / usable * 100, 1)
        return Reading(value=percent, unit="%", text=f"{percent:g} % used, {free_gb:g} GB free", fields=fields)
