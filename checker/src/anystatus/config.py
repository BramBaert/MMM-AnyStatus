"""Reading and validating the TOML config. Every problem is collected; an invalid config never half-starts."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter
from typing import Any

from anystatus.checks import REGISTRY, SECTIONS, Check
from anystatus.model import ID_PATTERN, Worse
from anystatus.table import ConfigError, Context, Table

DEFAULT_INTERVAL = 300.0
DEFAULT_TIMEOUT = 10.0
DEFAULT_CONFIRM = 2
DEFAULT_LISTEN = "127.0.0.1:8099"
DEFAULT_STATE = "anystatus.sqlite"
GENERIC_PLACEHOLDERS = frozenset({"value", "unit"})


@dataclass(frozen=True)
class Limits:
    warn: float | None
    crit: float | None
    worse: Worse


@dataclass(frozen=True)
class CheckSpec:
    id: str
    type: str
    label: str
    group: str | None
    interval: float
    timeout: float
    ttl: float
    confirm: int
    limits: Limits
    summary: str | None
    check: Check
    secrets: tuple[str, ...] = ()
    env_problem: str | None = (
        None  # a named environment variable is not set: every run is a failure with this text
    )


@dataclass(frozen=True)
class Config:
    name: str
    listen: tuple[str, int]
    state: Path
    checks: tuple[CheckSpec, ...]
    warnings: tuple[str, ...] = field(default=())
    sections: dict[str, Any] = field(default_factory=dict)


def parse_listen(text: str) -> tuple[str, int]:
    """ "host:port" or "[::1]:port"."""
    host, sep, port_text = text.rpartition(":")
    if not sep or not port_text.isdigit() or not 0 <= int(port_text) <= 65535:
        raise ValueError(f"'listen' must look like 127.0.0.1:8099, not {text!r}")
    host = host.strip("[]")
    if not host:
        raise ValueError("'listen' has no host")
    return host, int(port_text)


def template_fields(template: str) -> set[str]:
    """The placeholder names in a `summary` template. ValueError for broken syntax."""
    names: set[str] = set()
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name is not None:
            base = field_name.split(".")[0].split("[")[0]
            names.add(base)
    return names


def load_config(path: Path, env: Mapping[str, str] | None = None) -> Config:
    """Read `path`. Raises ConfigError listing every problem."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigError([f"cannot read {path}: {exc.strerror or exc}"]) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{path.name} is not valid TOML: {exc}"]) from exc
    return build_config(raw, path.resolve().parent, os.environ if env is None else env)


def build_config(raw: Mapping[str, Any], base_dir: Path, env: Mapping[str, str]) -> Config:
    ctx = Context(env=env, base_dir=base_dir)

    for key in raw:
        if key in SECTIONS:
            ctx.sections[key] = SECTIONS[key](raw[key], ctx)
        elif key not in ("checker", "defaults", "check"):
            ctx.errors.append(f"unknown section '{key}' (expected [checker], [defaults] and [[check]])")

    settings = Table(_as_dict(raw.get("checker", {}), "[checker]", ctx), "[checker]", ctx)
    name = settings.opt_str("name", "anystatus-checker") or "anystatus-checker"
    listen_text = settings.opt_str("listen", DEFAULT_LISTEN) or DEFAULT_LISTEN
    state_text = settings.opt_str("state", DEFAULT_STATE) or DEFAULT_STATE
    settings.finish()
    try:
        listen = parse_listen(listen_text)
    except ValueError as exc:
        ctx.errors.append(f"[checker]: {exc}")
        listen = parse_listen(DEFAULT_LISTEN)
    state = Path(state_text)
    if not state.is_absolute():
        state = base_dir / state

    defaults = Table(_as_dict(raw.get("defaults", {}), "[defaults]", ctx), "[defaults]", ctx)
    default_interval = defaults.duration("interval", DEFAULT_INTERVAL)
    default_timeout = defaults.duration("timeout", None)
    default_confirm = defaults.integer("confirm", DEFAULT_CONFIRM)
    defaults.finish()

    entries = raw.get("check", [])
    checks: list[CheckSpec] = []
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        ctx.errors.append("'check' must be an array of tables: [[check]]")
        entries = []
    elif not entries:
        ctx.errors.append("no checks configured: add at least one [[check]]")
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        spec = _build_check(
            entry, index, ctx, default_interval or DEFAULT_INTERVAL, default_timeout, default_confirm
        )
        if spec is None:
            continue
        if spec.id in seen:
            ctx.errors.append(f"check '{spec.id}': the id is used more than once")
        seen.add(spec.id)
        checks.append(spec)

    if ctx.errors:
        raise ConfigError(ctx.errors)
    return Config(name, listen, state, tuple(checks), tuple(ctx.warnings), dict(ctx.sections))


def _as_dict(value: Any, where: str, ctx: Context) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    ctx.errors.append(f"{where} must be a table")
    return {}


def _build_check(
    entry: Mapping[str, Any],
    index: int,
    ctx: Context,
    default_interval: float,
    default_timeout: float | None,
    default_confirm: int,
) -> CheckSpec | None:
    shown = entry.get("id")
    t = Table(entry, f"check '{shown}'" if isinstance(shown, str) else f"check #{index}", ctx)

    check_id = t.req_str("id")
    if check_id and not ID_PATTERN.match(check_id):
        t.error(
            "'id' may only use letters, digits and . _ : / - (at most 128 characters, starting with a letter or digit)"
        )
    type_name = t.req_str("type")
    label = t.opt_str("label", check_id) or check_id
    group = t.opt_str("group")
    interval = t.duration("interval", default_interval) or default_interval
    confirm = t.integer("confirm", default_confirm)
    timeout = t.duration("timeout") or default_timeout or min(DEFAULT_TIMEOUT, interval)
    if timeout > interval:
        t.warning(f"'timeout' is longer than the interval; using the interval ({interval:g} s)")
        timeout = interval
    ttl = t.duration("ttl", 3 * interval) or 3 * interval
    summary = t.opt_str("summary")

    check_cls = REGISTRY.get(type_name) if type_name else None
    if type_name and check_cls is None:
        t.error(f"unknown type '{type_name}' (known: {', '.join(sorted(REGISTRY))})")
    if check_cls is None:
        return None

    traits = check_cls.traits(t)
    warn_default, crit_default = traits.default_limits
    warn = t.limit("warn", warn_default, traits.duration_limits)
    crit = t.limit("crit", crit_default, traits.duration_limits)
    worse: Worse = "below" if t.choice("worse", ("above", "below"), traits.worse) == "below" else "above"
    if warn is not None and crit is not None and (warn >= crit if worse == "above" else warn <= crit):
        order = "warn < crit" if worse == "above" else "warn > crit"
        t.error(
            f'limits are out of order for worse = "{worse}": need {order}, got warn = {warn:g}, crit = {crit:g}'
        )

    if summary is not None:
        _check_template(
            t,
            summary,
            GENERIC_PLACEHOLDERS | traits.placeholders | ({"age"} if traits.duration_limits else set()),
        )

    check = check_cls.from_config(t)
    t.finish()
    env_problem = None
    if t.missing_env:
        names = ", ".join(dict.fromkeys(t.missing_env))
        env_problem = f"environment variable {names} not set"
        t.warning(env_problem)
    return CheckSpec(
        id=check_id,
        type=type_name,
        label=label,
        group=group,
        interval=interval,
        timeout=timeout,
        ttl=ttl,
        confirm=confirm,
        limits=Limits(warn, crit, worse),
        summary=summary,
        check=check,
        secrets=tuple(t.secrets),
        env_problem=env_problem,
    )


def _check_template(t: Table, template: str, allowed: set[str] | frozenset[str]) -> None:
    try:
        names = template_fields(template)
    except ValueError as exc:
        t.error(f"'summary' is not a valid template: {exc}")
        return
    unknown = sorted(n for n in names if n not in allowed)
    if unknown:
        t.error(
            f"'summary' uses unknown placeholder(s) {', '.join('{' + n + '}' for n in unknown)}; "
            f"available: {', '.join('{' + n + '}' for n in sorted(allowed))}"
        )
