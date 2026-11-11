"""Reading a value out of decoded JSON by a dotted path, and turning a value or a timestamp into a number or an age.

Shared by the `mqtt` check and (step 4b) the `http` check.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, tzinfo
from typing import Any

from anystatus.timeutil import age_seconds, parse_timestamp


def check_path(path: str) -> str | None:
    """A problem with a dotted path as written in a config, or None when it is usable."""
    if not path.strip():
        return "the path is empty"
    if any(not part for part in path.split(".")):
        return f"'{path}' has an empty part (a path looks like a.b.0.c)"
    return None


def dig(data: Any, path: str) -> Any:
    """The value at `a.b.0.c`: object keys by name, list items by number. LookupError when it is not there."""
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise LookupError(f"'{path}' not found")
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                raise LookupError(f"'{path}' not found")
            current = current[int(part)]
        else:
            raise LookupError(f"'{path}' not found")
    return current


def to_number(value: Any) -> float:
    """A finite number from a JSON number or a numeric string. ValueError otherwise."""
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError as exc:
            raise ValueError("not a number") from exc
    else:
        raise ValueError("not a number")
    if not math.isfinite(number):
        raise ValueError("not a number")
    return number


def to_time(value: Any, zone: tzinfo = UTC) -> datetime:
    """A moment from an RFC 3339 string or Unix seconds (a time without an offset is read in `zone`)."""
    return parse_timestamp(value, zone)


def to_age(value: Any, now: datetime, zone: tzinfo = UTC) -> float:
    """Seconds between a timestamp value and `now` (negative when the timestamp is in the future)."""
    return age_seconds(to_time(value, zone), now)
