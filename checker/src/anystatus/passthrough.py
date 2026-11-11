"""Serving results that came from a file as they are (the `result_file` type), and making sure that nothing served
fails SPEC.md: a result that is invalid is shown as `unknown` under its id, never dropped and never passed on broken.

The checker does not judge, debounce or re-stamp these results: `level`, `summary`, `value`, `unit`, `since`, `ts` and
`ttl` are the file's own. Only what the file leaves out is filled in (`label`, `group`), and what is wrong is replaced
by an `unknown` result with `ts` = when the file was read and `ttl` = the check's.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from anystatus.model import ID_PATTERN, LEVELS, Passthrough
from anystatus.timeutil import format_time

if TYPE_CHECKING:
    from anystatus.config import CheckSpec

# The schema's `time`: RFC 3339 with an explicit offset (fractions allowed).
_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})"
)
MAX_REASON = 120


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


class _Bad(Exception):
    """A field is wrong; the message is the reason that is served."""


def _text(raw: dict[str, Any], key: str, *, allow_empty: bool) -> str | None:
    """The text under `key`, None when absent. A value that is not a (non-empty) string is _Bad."""
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, str) or (not value and not allow_empty):
        raise _Bad(f"bad {key}")
    return value


def _valid_result(raw: dict[str, Any], spec: CheckSpec, result_id: str) -> dict[str, Any] | str:
    """The result as it will be served, or the reason it is invalid."""
    ts = _time(raw.get("ts"))
    if "ts" not in raw:
        return "no ts"
    if ts is None:
        return "bad ts (needs RFC 3339 with an offset)"
    if "ttl" not in raw:
        return "no ttl"
    ttl = raw["ttl"]
    if not _is_number(ttl) or ttl <= 0:
        return "bad ttl"
    level = raw.get("level")
    if "level" not in raw:
        return "no level"
    if not isinstance(level, str) or level not in LEVELS:
        return "bad level"
    try:
        label = _text(raw, "label", allow_empty=False)
        group = _text(raw, "group", allow_empty=False)
        summary = _text(raw, "summary", allow_empty=True)
        unit = _text(raw, "unit", allow_empty=True)
    except _Bad as exc:
        return str(exc)
    value = raw.get("value")
    if "value" in raw and not (isinstance(value, str) or _is_number(value)):
        return "bad value"
    since_text = raw.get("since")
    since = _time(since_text)
    if "since" in raw and since is None:
        return "bad since (needs RFC 3339 with an offset)"

    result: dict[str, Any] = {"id": result_id, "label": label or spec.label}
    group_text = group or spec.group
    if group_text:
        result["group"] = group_text
    result["level"] = level
    if summary:
        result["summary"] = summary
    if "value" in raw:
        result["value"] = value
    if unit:
        result["unit"] = unit
    if since is not None:
        result["since"] = raw["since"] if since <= ts else raw["ts"]  # SPEC: since is never later than ts
    result["ts"] = raw["ts"]
    result["ttl"] = ttl
    return result


def _invalid(
    spec: CheckSpec, result_id: str, reason: str, read_at: str, label: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {"id": result_id, "label": label or spec.label}
    if spec.group:
        result["group"] = spec.group
    result["level"] = "unknown"
    result["summary"] = f"invalid in file: {reason}"[:MAX_REASON]
    result["since"] = read_at
    result["ts"] = read_at
    result["ttl"] = int(spec.ttl) if float(spec.ttl).is_integer() else spec.ttl
    return result


def _result_id(spec: CheckSpec, raw: Any, prefixed: bool) -> tuple[str | None, str | None]:
    """The id a result is served under, or None and why that can't be done."""
    if not isinstance(raw, dict):
        return None, "not an object"
    if not prefixed:
        return spec.id, None
    own = raw.get("id")
    if "id" not in raw:
        return None, "no id"
    if not isinstance(own, str) or not ID_PATTERN.fullmatch(own):
        return None, "bad id"
    combined = f"{spec.id}/{own}"
    if not ID_PATTERN.fullmatch(combined):
        return None, "id too long" if len(combined) > 128 else "bad id"
    return combined, None


def render_passthrough(spec: CheckSpec, outcome: Passthrough, read_at: datetime) -> list[dict[str, Any]]:
    """The results to serve for one read of the file, in the file's order. Every one of them is valid per SPEC.md."""
    stamp = format_time(read_at)
    if outcome.prefixed and not outcome.entries:
        return [_invalid(spec, spec.id, "no results", stamp)]

    # served id -> [(raw entry, reason the id can't be used)]; None collects the entries without a usable id.
    groups: dict[str | None, list[tuple[Any, str | None]]] = {}
    for raw in outcome.entries:
        result_id, reason = _result_id(spec, raw, outcome.prefixed)
        groups.setdefault(result_id, []).append((raw, reason))

    served: list[dict[str, Any]] = []
    for result_id, members in groups.items():
        raw, reason = members[0]
        label = raw.get("label") if isinstance(raw, dict) else None
        label = label if isinstance(label, str) and label else None
        if result_id is None:
            text = reason or "bad id"
            if len(members) > 1:
                text = f"{len(members)} results without a usable id"
            served.append(_invalid(spec, spec.id, text, stamp))
        elif len(members) > 1:
            served.append(_invalid(spec, result_id, "id used more than once", stamp))
        else:
            checked = _valid_result(raw, spec, result_id)
            if isinstance(checked, str):
                served.append(_invalid(spec, result_id, checked, stamp, label))
            else:
                served.append(checked)
    return served
