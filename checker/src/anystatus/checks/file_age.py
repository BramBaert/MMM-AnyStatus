"""`file_age`: how old is the newest file that matches a glob ("did last night's backup land?")."""

from __future__ import annotations

import glob
import os
import re
import time
from pathlib import Path

from anystatus.checks.base import Check, register
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.table import Table
from anystatus.timeutil import format_duration

# Decimal units, as disks and backup tools count them: 1KB = 1000 bytes.
_UNITS = {"": 1, "b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4}
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*$", re.IGNORECASE)
_MAGIC = re.compile(r"[*?\[]")


def parse_size(value: object) -> int:
    """A size in bytes: a whole number of bytes, or text such as "500KB", "1MB", "1.5GB" (decimal: KB, MB, GB, TB).
    Raises ValueError."""
    if isinstance(value, bool):
        raise ValueError("not a size")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("a size cannot be negative")
        return value
    if isinstance(value, str):
        m = _SIZE.match(value)
        if m and m.group(2).lower() in _UNITS:
            return int(float(m.group(1)) * _UNITS[m.group(2).lower()])
    raise ValueError('not a size (use bytes, or for example "500KB", "1MB", "2GB"; KB = 1000 bytes)')


@register
class FileAge(Check):
    type_name = "file_age"
    duration_limits = True
    placeholders = frozenset({"file"})

    def __init__(self, pattern: str, min_size: int) -> None:
        self.pattern = pattern
        self.min_size = min_size

    @classmethod
    def from_config(cls, table: Table) -> FileAge:
        text = table.req_str("path")
        # A relative pattern is relative to the config file; the folder itself is never read as a pattern.
        pattern = (
            text if not text or Path(text).is_absolute() else glob.escape(str(table.base_dir)) + os.sep + text
        )
        min_size = 0
        if table.has("min_size"):
            try:
                min_size = parse_size(table.raw("min_size"))
            except ValueError as exc:
                table.error(f"'min_size': {exc}")
        return cls(pattern, min_size)

    def run(self, timeout: float) -> Observation | Reading:
        folder = os.path.dirname(self.pattern)
        if folder and not _MAGIC.search(folder):
            # glob() hides errors: an unreadable folder would look like "no file matches". A folder that is not
            # there is that answer (a mount that is gone); one that can't be read is not.
            try:
                os.listdir(folder)
            except (FileNotFoundError, NotADirectoryError):
                return Observation(level="crit", summary="no file matches")
            except OSError as exc:
                raise CheckFailure("cannot read the folder", detail=f"{type(exc).__name__}: {exc}") from exc
        newest: tuple[float, str] | None = None
        for name in glob.glob(self.pattern, recursive=True):
            try:
                info = os.stat(name)
            except OSError:
                continue  # gone since the listing
            if not os.path.isfile(name) or info.st_size < self.min_size:
                continue
            if newest is None or (info.st_mtime, name) > newest:
                newest = (info.st_mtime, name)
        if newest is None:
            return Observation(level="crit", summary="no file matches")
        age = max(0.0, time.time() - newest[0])  # a modification time in the future counts as just now
        shown, unit = format_duration(age)
        return Reading(
            value=age,
            seconds=True,
            text=f"newest file is {shown:.1f} {unit} old",
            fields={"file": os.path.basename(newest[1])},
        )
