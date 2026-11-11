from __future__ import annotations

from pathlib import Path

import pytest
from helpers import parse

from anystatus.config import Limits
from anystatus.judge import judge, level_for
from anystatus.model import Failure, Observation, Reading
from anystatus.table import ConfigError


def test_higher_is_worse_and_limits_are_inclusive() -> None:
    limits = Limits(warn=10, crit=20, worse="above")
    assert [level_for(v, limits) for v in (9.99, 10, 19.99, 20, 500)] == [
        "ok",
        "warn",
        "warn",
        "crit",
        "crit",
    ]


def test_lower_is_worse_and_limits_are_inclusive() -> None:
    limits = Limits(warn=10, crit=5, worse="below")
    assert [level_for(v, limits) for v in (500, 10.01, 10, 5.01, 5, -1)] == [
        "ok",
        "ok",
        "warn",
        "warn",
        "crit",
        "crit",
    ]


def test_either_limit_may_be_left_out() -> None:
    assert level_for(99, Limits(None, 20, "above")) == "crit"
    assert level_for(15, Limits(None, 20, "above")) == "ok"
    assert level_for(15, Limits(10, None, "above")) == "warn"
    assert level_for(15, Limits(None, None, "above")) == "ok"


def spec_for(extra: str, tmp_path: Path):  # type: ignore[no-untyped-def]
    text = f'[[check]]\nid = "x"\ntype = "python"\nfunction = "support_checks:number"\n{extra}\n'
    return parse(text, tmp_path).checks[0]


def test_worse_override_and_ordering_per_direction(tmp_path: Path) -> None:
    spec = spec_for('warn = 2\ncrit = 0.5\nworse = "below"', tmp_path)
    assert spec.limits == Limits(2, 0.5, "below")
    with pytest.raises(ConfigError, match="out of order"):
        spec_for('warn = 0.5\ncrit = 2\nworse = "below"', tmp_path)
    with pytest.raises(ConfigError, match="out of order"):
        spec_for("warn = 5\ncrit = 1", tmp_path)  # above is the default
    with pytest.raises(ConfigError, match="out of order"):
        spec_for("warn = 5\ncrit = 5", tmp_path)  # equal is not "in order" either


def test_duration_limits_only_for_duration_checks(tmp_path: Path) -> None:
    sql = """
[[check]]
id = "s"
type = "sql_age"
database = "sqlite:x.db"
query = "select 1"
warn = "26h"
crit = "2d"
"""
    assert parse(sql, tmp_path).checks[0].limits == Limits(26 * 3600, 2 * 86400, "above")
    with pytest.raises(ConfigError, match="'warn' must be a number"):
        spec_for('warn = "26h"', tmp_path)


def test_a_reading_becomes_a_level_and_a_duration_is_reported_in_its_unit(tmp_path: Path) -> None:
    text = """
[[check]]
id = "s"
type = "sql_age"
database = "sqlite:x.db"
query = "select 1"
warn = "26h"
crit = "50h"
"""
    spec = parse(text, tmp_path).checks[0]
    core = judge(spec, Reading(value=3.1 * 86400, seconds=True, text="newest entry 3.1 d ago"))
    assert (core.level, core.value, core.unit, core.summary) == ("crit", 3.1, "d", "newest entry 3.1 d ago")
    assert judge(spec, Reading(value=30 * 3600, seconds=True)).level == "warn"
    assert judge(spec, Reading(value=3600, seconds=True)).level == "ok"


def test_failures_are_unknown_never_crit(tmp_path: Path) -> None:
    spec = spec_for("crit = 1", tmp_path)
    assert judge(spec, Failure("connection refused")).level == "unknown"


def test_own_level_ignores_limits_and_an_unknown_level_is_unknown(tmp_path: Path) -> None:
    spec = spec_for("crit = 1", tmp_path)
    assert judge(spec, Observation(level="ok", value=99)).level == "ok"
    assert judge(spec, Observation(level="purple")).level == "unknown"


def test_summary_template(tmp_path: Path) -> None:
    text = """
[[check]]
id = "s"
type = "sql_age"
database = "sqlite:x.db"
query = "select 1"
summary = "last backup {age} ago ({value} {unit}), newest {newest}"
"""
    spec = parse(text, tmp_path).checks[0]
    core = judge(spec, Reading(value=5 * 3600, seconds=True, fields={"newest": "2026-11-11 10:00"}))
    assert core.summary == "last backup 5.0 h ago (5.0 h), newest 2026-11-11 10:00"
