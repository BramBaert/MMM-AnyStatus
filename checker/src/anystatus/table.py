"""A config table that knows which keys have been read, so unknown keys can be reported, and that collects problems."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anystatus.timeutil import load_timezone, parse_duration

_MISSING = object()
_SECRET_NAMES = ("password", "passwd", "secret", "token", "bearer", "api_key", "apikey")


class ConfigError(Exception):
    """The config is invalid. `problems` lists every one of them."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass
class Context:
    """Shared by every table of one config file."""

    env: Mapping[str, str]
    base_dir: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sections: dict[str, Any] = field(default_factory=dict)  # parsed top-level sections owned by check types


class Table:
    def __init__(self, data: Mapping[str, Any], where: str, ctx: Context) -> None:
        self.data = dict(data)
        self.where = where
        self.ctx = ctx
        self.used: set[str] = set()
        self.secrets: list[str] = []  # values read from the environment: masked in logs
        self.missing_env: list[str] = []  # environment variables that were named but are not set

    # problems

    def error(self, message: str) -> None:
        self.ctx.errors.append(f"{self.where}: {message}")

    def warning(self, message: str) -> None:
        self.ctx.warnings.append(f"{self.where}: {message}")

    @property
    def base_dir(self) -> Path:
        return self.ctx.base_dir

    # reading

    def has(self, key: str) -> bool:
        return key in self.data

    def _take(self, key: str) -> Any:
        self.used.add(key)
        return self.data.get(key, _MISSING)

    def req_str(self, key: str) -> str:
        value = self.opt_str(key)
        if value is None:
            self.error(f"missing '{key}'")
            return ""
        return value

    def opt_str(self, key: str, default: str | None = None) -> str | None:
        value = self._take(key)
        if value is _MISSING:
            return default
        if not isinstance(value, str) or not value.strip():
            self.error(f"'{key}' must be a non-empty string")
            return default
        return value

    def integer(self, key: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
        value = self._take(key)
        if value is _MISSING:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            self.error(f"'{key}' must be a whole number")
            return default
        if value < minimum or (maximum is not None and value > maximum):
            bounds = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
            self.error(f"'{key}' must be {bounds}")
            return default
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value = self._take(key)
        if value is _MISSING:
            return default
        if not isinstance(value, bool):
            self.error(f"'{key}' must be true or false")
            return default
        return value

    def str_map(self, key: str) -> dict[str, str] | None:
        """A table of string -> string, None when the key is not there."""
        value = self._take(key)
        if value is _MISSING:
            return None
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            self.error(f"'{key}' must be a table of text values, such as {{ Online = \"ok\" }}")
            return None
        return dict(value)

    def duration(self, key: str, default: float | None = None) -> float | None:
        """A duration greater than zero."""
        value = self._take(key)
        if value is _MISSING:
            return default
        try:
            seconds = parse_duration(value)
        except ValueError as exc:
            self.error(f"'{key}': {exc}")
            return default
        if seconds <= 0:
            self.error(f"'{key}' must be greater than zero")
            return default
        return seconds

    def limit(self, key: str, default: float | None, as_duration: bool) -> float | None:
        """A threshold: a number, or for duration-valued checks also a duration string."""
        value = self._take(key)
        if value is _MISSING:
            return default
        if isinstance(value, bool):
            self.error(f"'{key}' must be a number")
            return default
        if isinstance(value, int | float):
            if not math.isfinite(value):
                self.error(f"'{key}' must be a finite number")
                return default
            return float(value)
        if as_duration and isinstance(value, str):
            try:
                return parse_duration(value)
            except ValueError as exc:
                self.error(f"'{key}': {exc}")
                return default
        self.error(f"'{key}' must be a number" + (' or a duration such as "26h"' if as_duration else ""))
        return default

    def choice(self, key: str, options: Sequence[str], default: str) -> str:
        value = self._take(key)
        if value is _MISSING:
            return default
        if not isinstance(value, str) or value not in options:
            self.error(f"'{key}' must be one of: {', '.join(options)}")
            return default
        return value

    def timezone(self, key: str = "timezone") -> Any:
        """A tzinfo, UTC when not given."""
        name = self.opt_str(key, "UTC") or "UTC"
        try:
            return load_timezone(name)
        except ValueError as exc:
            self.error(f"'{key}': {exc}")
            return load_timezone("UTC")

    def env(self, key: str) -> str | None:
        """The secret named by `<key>_env`. None when not configured or when the variable isn't set (then the check
        reports `unknown`, see CheckSpec.env_problem)."""
        name = self.opt_str(f"{key}_env")
        if name is None:
            return None
        value = self.ctx.env.get(name)
        if value is None:
            self.missing_env.append(name)
            return None
        if value:
            self.secrets.append(value)
        return value

    def rest(self) -> dict[str, Any]:
        """Every key not read so far (for the `python` check). `x_env` keys are replaced by `x` and the variable's value."""
        out: dict[str, Any] = {}
        for key, value in self.data.items():
            if key in self.used:
                continue
            self.used.add(key)
            if key.endswith("_env") and len(key) > 4:
                name = value if isinstance(value, str) else ""
                resolved = self.ctx.env.get(name)
                if resolved is None:
                    self.missing_env.append(name or key)
                else:
                    if resolved:
                        self.secrets.append(resolved)
                    out[key[:-4]] = resolved
            else:
                out[key] = value
        return out

    def finish(self) -> None:
        """Whatever was not read is an unknown key: a typo must not fall back to a default silently."""
        for key in self.data:
            if key in self.used:
                continue
            hint = ""
            if key.lower() in _SECRET_NAMES:
                hint = f" (secrets never sit in the file: use '{key}_env' to name an environment variable)"
            self.error(f"unknown key '{key}'{hint}")
