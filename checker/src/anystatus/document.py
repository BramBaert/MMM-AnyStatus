"""The results document (SPEC.md) and the in-memory snapshot the HTTP server answers from."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from anystatus.model import Reported
from anystatus.timeutil import format_time

if TYPE_CHECKING:
    from anystatus.config import CheckSpec

SPEC_VERSION = "1.0"


def render_result(spec: CheckSpec, reported: Reported) -> dict[str, Any]:
    """One result of the document. `ts` is when the check ran, never when this is served."""
    result: dict[str, Any] = {"id": spec.id, "label": spec.label}
    if spec.group:
        result["group"] = spec.group
    result["level"] = reported.core.level
    if reported.core.summary:
        result["summary"] = reported.core.summary
    if reported.core.value is not None:
        result["value"] = reported.core.value
    if reported.core.unit:
        result["unit"] = reported.core.unit
    # SPEC: since must not be later than ts; times are whole seconds, so compare what is written.
    since = min(reported.since, reported.ts)
    result["since"] = format_time(since)
    result["ts"] = format_time(reported.ts)
    result["ttl"] = int(spec.ttl) if float(spec.ttl).is_integer() else spec.ttl
    return result


def render_document(source: str, results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {"spec": SPEC_VERSION, "source": source, "results": list(results)}


def render_overview(source: str, results: Sequence[dict[str, Any]]) -> str:
    """A plain-text page for humans."""
    lines = [f"{source}: {len(results)} check(s)", ""]
    width = max((len(str(r["id"])) for r in results), default=2)
    for r in results:
        lines.append(f"{r['level'].upper():8} {r['id']!s:{width}}  {r.get('summary', '')}  [{r['ts']}, ttl {r['ttl']} s]")
    lines += ["", "GET /results for the JSON document."]
    return "\n".join(lines) + "\n"


class Snapshot:
    """The current results, replaced as a whole by the scheduler after every run; read by the HTTP threads."""

    def __init__(self, source: str) -> None:
        self.source = source
        self._lock = threading.Lock()
        self._json = b""
        self._overview = b""
        self.replace([])

    def replace(self, results: Sequence[dict[str, Any]]) -> None:
        body = json.dumps(render_document(self.source, results), ensure_ascii=False).encode("utf-8")
        overview = render_overview(self.source, results).encode("utf-8")
        with self._lock:
            self._json, self._overview = body, overview

    def json_bytes(self) -> bytes:
        with self._lock:
            return self._json

    def overview_bytes(self) -> bytes:
        with self._lock:
            return self._overview
