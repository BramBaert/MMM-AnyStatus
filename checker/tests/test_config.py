from __future__ import annotations

from pathlib import Path

import pytest
from helpers import parse

from anystatus.config import template_fields
from anystatus.model import Failure, Observation
from anystatus.scheduler import execute
from anystatus.table import ConfigError
from anystatus.timeutil import format_duration, parse_duration

GOOD = """
[checker]
name = "t"
[defaults]
interval = "5m"
confirm = 3
[[check]]
id = "a"
type = "scripted"
"""


def problems(text: str, tmp_path: Path, env: dict[str, str] | None = None) -> list[str]:
    with pytest.raises(ConfigError) as info:
        parse(text, tmp_path, env)
    return info.value.problems


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (30, 30.0),
        (1.5, 1.5),
        ("30s", 30.0),
        ("5m", 300.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        ("1h30m", 5400.0),
    ],
)
def test_durations(value: object, seconds: float) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "5", "5x", "m5", "-1", -1, True, None, "1h 30"])
def test_bad_durations(value: object) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(3.1 * 86400, (3.1, "d")), (5 * 3600, (5.0, "h")), (12 * 60, (12.0, "min")), (40, (40.0, "s"))],
)
def test_duration_reporting_uses_the_largest_unit(seconds: float, expected: tuple[float, str]) -> None:
    assert format_duration(seconds) == expected


def test_defaults_apply_and_ttl_is_three_intervals(tmp_path: Path) -> None:
    spec = parse(GOOD, tmp_path).checks[0]
    assert (spec.interval, spec.confirm, spec.ttl, spec.label) == (300.0, 3, 900.0, "a")
    assert spec.timeout == 10.0


def test_a_check_overrides_defaults(tmp_path: Path) -> None:
    text = GOOD + 'interval = "30s"\nconfirm = 1\nttl = "10m"\nlabel = "Nice"\ngroup = "g"\n'
    spec = parse(text, tmp_path).checks[0]
    assert (spec.interval, spec.confirm, spec.ttl, spec.label, spec.group) == (30.0, 1, 600.0, "Nice", "g")


def test_unknown_keys_are_errors(tmp_path: Path) -> None:
    errs = problems(GOOD + 'intervall = "1m"\n', tmp_path)
    assert errs == ["check 'a': unknown key 'intervall'"]


def test_all_problems_are_listed_together(tmp_path: Path) -> None:
    text = """
[checker]
listen = "nonsense"
colour = "red"
[defaults]
confirm = 0
[[check]]
id = "a"
type = "nosuchtype"
[[check]]
id = "b"
type = "scripted"
interval = "soon"
warn = 5
crit = 1
bogus = true
[[check]]
id = "b"
type = "scripted"
"""
    errs = problems(text, tmp_path)
    joined = "\n".join(errs)
    assert len(errs) >= 7
    for fragment in (
        "listen",
        "colour",
        "confirm",
        "nosuchtype",
        "interval",
        "out of order",
        "bogus",
        "more than once",
    ):
        assert fragment in joined


def test_no_checks_is_an_error(tmp_path: Path) -> None:
    assert "no checks configured" in problems("[checker]\nname='x'\n", tmp_path)[0]


def test_bad_id_and_unknown_section(tmp_path: Path) -> None:
    errs = problems('[[check]]\nid = "-bad id"\ntype = "scripted"\n[extra]\nx = 1\n', tmp_path)
    assert any("'id' may only use" in e for e in errs)
    assert any("unknown section 'extra'" in e for e in errs)


def test_timeout_never_exceeds_the_interval(tmp_path: Path) -> None:
    short = parse(GOOD + 'interval = "5s"\n', tmp_path).checks[0]
    assert short.timeout == 5.0  # default timeout is capped by the interval
    config = parse(GOOD + 'interval = "30s"\ntimeout = "1h"\n', tmp_path)
    assert config.checks[0].timeout == 30.0
    assert any("timeout" in w for w in config.warnings)


def test_missing_env_variable_is_a_warning_and_the_result_is_unknown(tmp_path: Path) -> None:
    text = (
        GOOD
        + """
[[check]]
id = "needs.env"
type = "python"
function = "support_checks:number"
token_env = "SOME_TOKEN"
"""
    )
    config = parse(text, tmp_path, env={})
    assert config.warnings == ("check 'needs.env': environment variable SOME_TOKEN not set",)
    spec = config.checks[1]
    assert execute(spec) == Failure("environment variable SOME_TOKEN not set")


def test_env_variable_is_read_and_passed_on(tmp_path: Path) -> None:
    text = """
[[check]]
id = "e"
type = "python"
function = "support_checks:echo_config"
site = "roof"
token_env = "SOME_TOKEN"
"""
    config = parse(text, tmp_path, env={"SOME_TOKEN": "s3cret"})
    outcome = execute(config.checks[0])
    assert isinstance(outcome, Observation)
    assert outcome.summary == "[('site', 'roof'), ('token', 's3cret')]"
    assert config.checks[0].secrets == ("s3cret",)


def test_a_secret_written_in_the_file_is_pointed_at_env(tmp_path: Path) -> None:
    text = """
[[check]]
id = "q"
type = "sql_age"
database = "sqlite:x.db"
query = "select 1"
password = "hunter2"
"""
    assert "use 'password_env'" in problems(text, tmp_path)[0]


def test_summary_template_placeholders_are_checked(tmp_path: Path) -> None:
    errs = problems(GOOD + 'summary = "{value} {nope}"\n', tmp_path)
    assert "{nope}" in errs[0]
    ok = parse(GOOD + 'summary = "{value}{unit}"\n', tmp_path)
    assert ok.checks[0].summary == "{value}{unit}"
    assert template_fields("last backup {age} ago, {newest:>5}") == {"age", "newest"}


def test_listen_is_parsed(tmp_path: Path) -> None:
    assert parse(GOOD, tmp_path).listen == ("127.0.0.1", 8099)
    config = parse(GOOD.replace('name = "t"', 'name = "t"\nlisten = "[::1]:9000"'), tmp_path)
    assert config.listen == ("::1", 9000)
