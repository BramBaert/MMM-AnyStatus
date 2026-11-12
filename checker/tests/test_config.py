from __future__ import annotations

from pathlib import Path

import pytest
from helpers import parse

from anystatus.config import template_fields
from anystatus.model import NEVER, Failure, Observation, ShowAfter
from anystatus.scheduler import execute
from anystatus.table import ConfigError
from anystatus.timeutil import format_duration, parse_duration, parse_wait

GOOD = """
[checker]
name = "t"
[defaults]
interval = "5m"
show_after = { unknown = "10m", warn = "1h" }
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
    assert (spec.interval, spec.ttl, spec.label) == (300.0, 900.0, "a")
    assert spec.show_after == ShowAfter(warn=3600, crit=0, unknown=600)  # crit: no time anywhere, so at once
    assert spec.timeout == 10.0


def test_a_check_overrides_defaults(tmp_path: Path) -> None:
    text = GOOD + 'interval = "30s"\nshow_after = { warn = "0d", crit = "never" }\nttl = "10m"\nlabel = "Nice"\ngroup = "g"\n'
    spec = parse(text, tmp_path).checks[0]
    assert (spec.interval, spec.ttl, spec.label, spec.group) == (30.0, 600.0, "Nice", "g")
    # per level: the check's own times win, the default's other levels stay
    assert spec.show_after == ShowAfter(warn=0, crit=NEVER, unknown=600)


def test_no_show_after_means_at_once(tmp_path: Path) -> None:
    spec = parse('[[check]]\nid = "a"\ntype = "scripted"\n', tmp_path).checks[0]
    assert spec.show_after == ShowAfter(0, 0, 0)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("0d", 0.0), ("2m", 120.0), ("14d", 14 * 86400.0), ("1d12h", 1.5 * 86400), ("30s", 30.0), ("never", NEVER), ("Never", NEVER)],
)
def test_show_after_times(value: str, seconds: float) -> None:
    assert parse_wait(value) == seconds


@pytest.mark.parametrize("value", ["1w", "1y", "2mo", "5", "", "soon", 5, True])
def test_bad_show_after_times(value: object) -> None:
    with pytest.raises(ValueError):
        parse_wait(value)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('show_after = { warn = "2w" }', "'show_after.warn': \"2w\" is not a time"),
        ("show_after = { warn = 60 }", "'show_after.warn': must be a time"),
        ('show_after = { ok = "1m" }', "'ok' is not a level that can wait"),
        ('show_after = "2m"', "'show_after' must be a table"),
    ],
)
def test_show_after_errors(tmp_path: Path, line: str, message: str) -> None:
    errs = problems(f'[[check]]\nid = "a"\ntype = "scripted"\n{line}\n', tmp_path)
    assert any(message in e for e in errs), errs


def test_confirm_is_gone_and_says_what_replaced_it(tmp_path: Path) -> None:
    for text in ('[[check]]\nid = "a"\ntype = "scripted"\nconfirm = 2\n', '[defaults]\nconfirm = 2\n[[check]]\nid = "a"\ntype = "scripted"\n'):
        (err,) = problems(text, tmp_path)
        assert "'confirm' has been replaced by 'show_after'" in err


def test_a_show_after_shorter_than_the_interval_is_a_warning(tmp_path: Path) -> None:
    text = '[[check]]\nid = "a"\ntype = "scripted"\ninterval = "5m"\nshow_after = { unknown = "2m", warn = "0d", crit = "5m" }\n'
    config = parse(text, tmp_path)
    assert config.warnings == ("check 'a': 'show_after.unknown' (2m) is shorter than the interval (5m): it means at the next run",)
    # [defaults] is compared with the default interval, not with every check's own
    text = '[defaults]\ninterval = "1m"\nshow_after = { unknown = "30s", warn = "2m" }\n[[check]]\nid = "a"\ntype = "scripted"\ninterval = "1d"\n'
    assert parse(text, tmp_path).warnings == ("[defaults]: 'show_after.unknown' (30s) is shorter than the interval (1m): it means at the next run",)


def test_unknown_keys_are_errors(tmp_path: Path) -> None:
    errs = problems(GOOD + 'intervall = "1m"\n', tmp_path)
    assert errs == ["check 'a': unknown key 'intervall'"]


def test_all_problems_are_listed_together(tmp_path: Path) -> None:
    text = """
[checker]
listen = "nonsense"
colour = "red"
[defaults]
show_after = { warn = "soon" }
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
        "show_after.warn",
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
