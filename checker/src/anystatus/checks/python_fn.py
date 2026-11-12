"""`python`: your own function, the escape hatch for anything else.

The function gets the check's config table (a dict of every key the checker doesn't use itself) and returns a number,
which the limits turn into a level, or `anystatus.Observation(...)` when it decides itself. It may be `async`.
An exception is a failure; its type, not its message, goes into the summary.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import math
import sys
import traceback
from collections.abc import Callable
from typing import Any

from anystatus.checks.base import Check, register
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.table import Table


@register
class PythonCheck(Check):
    type_name = "python"

    def __init__(self, function: Callable[[dict[str, Any]], Any], settings: dict[str, Any], name: str) -> None:
        self.function = function
        self.settings = settings
        self.name = name

    @classmethod
    def from_config(cls, table: Table) -> PythonCheck:
        spec = table.req_str("function")
        path = table.opt_str("path")
        settings = table.rest()
        function = _import_function(table, spec, path)
        return cls(function, settings, spec)

    def run(self, timeout: float) -> Observation | Reading:
        try:
            result = self.function(dict(self.settings))
            if inspect.isawaitable(result):
                result = asyncio.run(_await(result))
        except Exception as exc:
            raise CheckFailure(f"function raised {type(exc).__name__}", detail=traceback.format_exc()) from exc
        if isinstance(result, Observation):
            return result
        if isinstance(result, int | float) and not isinstance(result, bool):
            if not math.isfinite(result):
                raise CheckFailure("function returned a value that is not a finite number")
            return Reading(value=float(result), text=f"{result:g}")
        raise CheckFailure(f"function returned {type(result).__name__}, expected a number or Observation")


async def _await(awaitable: Any) -> Any:
    return await awaitable


def _import_function(table: Table, spec: str, path: str | None) -> Callable[[dict[str, Any]], Any]:
    def fail(message: str) -> Callable[[dict[str, Any]], Any]:
        table.error(message)
        return lambda _cfg: None

    if not spec:
        return fail("missing 'function'")
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        return fail(f"'function' must be 'module:function', not {spec!r}")
    if path:
        directory = str((table.base_dir / path).resolve())
        if directory not in sys.path:
            sys.path.insert(0, directory)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # user code runs at import: anything can go wrong
        return fail(f"cannot import '{module_name}' ({type(exc).__name__}: {exc})")
    function = getattr(module, attr, None)
    if not callable(function):
        return fail(f"'{module_name}' has no function '{attr}'")
    return function  # type: ignore[no-any-return]
