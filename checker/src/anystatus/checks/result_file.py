"""`result_file`: another program writes a SPEC result (or a whole document) to a file, and the checker serves it.

Any language can report this way without a push endpoint. The results are passed through as they are (see
passthrough.py): not judged, not debounced, not re-stamped. Only a file that can't be read at all is a failure.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from anystatus.checks.base import Check, register
from anystatus.model import CheckFailure, Passthrough
from anystatus.table import Table

MAX_FILE = 1024 * 1024  # bytes
_SPEC = re.compile(r"^(\d+)\.(\d+)$")
_RETRY_AFTER = 0.1  # a writer that doesn't rename into place may be half way through: look once more


@register
class ResultFile(Check):
    type_name = "result_file"

    def __init__(self, path: str) -> None:
        self.path = path

    @classmethod
    def from_config(cls, table: Table) -> ResultFile:
        for key in ("warn", "crit", "worse", "summary"):
            if table.has(key):
                table.error(f"result_file serves the file's own results: '{key}' is not used")
        return cls(table.path("path"))

    def _read(self) -> Any:
        try:
            with open(self.path, "rb") as handle:
                data = handle.read(MAX_FILE + 1)
        except FileNotFoundError as exc:
            raise CheckFailure("file not found", detail=str(exc)) from exc
        except OSError as exc:
            raise CheckFailure("cannot read the file", detail=f"{type(exc).__name__}: {exc}") from exc
        if len(data) > MAX_FILE:
            raise CheckFailure(f"file is larger than {MAX_FILE // (1024 * 1024)} MB")
        try:
            return json.loads(data.decode("utf-8-sig"))
        except ValueError as exc:  # includes UnicodeDecodeError
            raise CheckFailure("not JSON", detail=f"{type(exc).__name__}: {exc}") from exc

    def run(self, timeout: float) -> Passthrough:
        try:
            content = self._read()
        except CheckFailure as failure:
            if failure.summary != "not JSON":
                raise
            time.sleep(_RETRY_AFTER)
            content = self._read()
        if not isinstance(content, dict):
            raise CheckFailure("not a result or a document")
        if "spec" not in content and "results" not in content:
            return Passthrough((content,), prefixed=False)  # a single result
        version = content.get("spec")
        match = _SPEC.match(version) if isinstance(version, str) else None
        if match is None:
            raise CheckFailure("no valid spec version in the document")
        if match.group(1) != "1":
            raise CheckFailure(f"unsupported spec {version}")
        results = content.get("results")
        if not isinstance(results, list):
            raise CheckFailure("the document has no results list")
        return Passthrough(tuple(results), prefixed=True)
