"""Durations and timestamps: parsing config values, reading database values, writing SPEC times."""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UNITS = {"w": 604800.0, "d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}
_PART = re.compile(r"(\d+(?:\.\d+)?)([wdhms])")


def parse_duration(value: object) -> float:
    """A number of seconds, or a string such as "30s", "5m", "2h", "1d", "1h30m". Raises ValueError."""
    if isinstance(value, bool):
        raise ValueError("not a duration")
    if isinstance(value, int | float):
        if value < 0 or not math.isfinite(value):
            raise ValueError("a duration cannot be negative")
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower()
        pos = 0
        total = 0.0
        while pos < len(text):
            m = _PART.match(text, pos)
            if m is None:
                raise ValueError(f'"{value}" is not a duration (use for example "30s", "5m", "2h", "1h30m")')
            total += float(m.group(1)) * _UNITS[m.group(2)]
            pos = m.end()
        if pos == 0:
            raise ValueError("an empty duration")
        return total
    raise ValueError("not a duration")


_WAIT_PART = re.compile(r"(\d+(?:\.\d+)?)([dhms])")


def parse_wait(value: object) -> float:
    """A `show_after` or schedule time: "never" (math.inf), or a duration in s, m, h, d only ("0d", "2m", "1d12h").
    No weeks, months or years, and no bare numbers: the unit is always written. Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError('must be a time such as "2m", "14d" or "0d", or "never"')
    text = value.strip().lower()
    if text == "never":
        return math.inf
    pos, total = 0, 0.0
    while pos < len(text):
        m = _WAIT_PART.match(text, pos)
        if m is None:
            break
        total += float(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if not text or pos != len(text):
        raise ValueError(f'"{value}" is not a time: use s, m, h or d, such as "2m", "14d", "0d" (at once), or "never"')
    return total


def format_wait(seconds: float) -> str:
    """A `show_after` time as written back in messages: "never", "2m", "1d", "90s"."""
    if math.isinf(seconds):
        return "never"
    for size, unit in ((86400.0, "d"), (3600.0, "h"), (60.0, "m")):
        if seconds >= size and seconds % size == 0:
            return f"{seconds / size:g}{unit}"
    return f"{seconds:g}s"


def format_duration(seconds: float) -> tuple[float, str]:
    """The largest unit that fits, one decimal: 3.1 d, 5.0 h, 12.0 min, 40.0 s."""
    magnitude = abs(seconds)
    for limit, unit in ((86400.0, "d"), (3600.0, "h"), (60.0, "min")):
        if magnitude >= limit:
            return round(seconds / limit, 1), unit
    return round(seconds, 1), "s"


def format_time(moment: datetime) -> str:
    """A SPEC time: RFC 3339, UTC, whole seconds."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime:
    """Read back a time written by format_time (or any RFC 3339 time with an offset)."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("no offset")
    return moment.astimezone(UTC)


def load_timezone(name: str) -> tzinfo:
    """An IANA time zone name. UTC needs no database."""
    if name.upper() in ("UTC", "Z"):
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f'unknown time zone "{name}"') from exc


_NUMERIC = re.compile(r"^\d+(\.\d+)?$")


def parse_timestamp(value: Any, zone: tzinfo = UTC) -> datetime:
    """A timestamp as a database or a file gives it: a time type, an RFC 3339 string or Unix seconds.

    A time without an offset is read in `zone`. Raises ValueError for anything else.
    """
    if isinstance(value, bool):
        raise ValueError("not a timestamp")
    if isinstance(value, datetime):
        return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=zone).astimezone(UTC)
    if isinstance(value, int | float | Decimal):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("not a timestamp")
        return datetime.fromtimestamp(number, UTC)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if _NUMERIC.match(text):
            return datetime.fromtimestamp(float(text), UTC)
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("unrecognised timestamp format") from exc
        return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(UTC)
    raise ValueError("not a timestamp")


def age_seconds(moment: datetime, now: datetime) -> float:
    return (now - moment) / timedelta(seconds=1)
