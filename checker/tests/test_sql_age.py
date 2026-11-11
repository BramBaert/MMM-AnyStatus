from __future__ import annotations

import sqlite3
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import parse

from anystatus.checks import sql_age
from anystatus.checks.database import fetch_first_row, parse_database_url
from anystatus.judge import judge
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.scheduler import execute
from anystatus.table import ConfigError

NOW = datetime(2026, 11, 11, 10, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sql_age, "_now", lambda: NOW)


def make_db(tmp_path: Path, *values: object) -> Path:
    path = tmp_path / "log.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs (finished_at, ok)")
    conn.executemany("INSERT INTO runs VALUES (?, 1)", [(v,) for v in values])
    conn.commit()
    conn.close()
    return path


def spec_for(tmp_path: Path, extra: str = "", query: str = "SELECT max(finished_at) FROM runs"):  # type: ignore[no-untyped-def]
    text = f"""
[[check]]
id = "backup"
type = "sql_age"
database = "sqlite:log.db"
query = "{query}"
{extra}
"""
    return parse(text, tmp_path).checks[0]


def run(spec):  # type: ignore[no-untyped-def]
    outcome = spec.check.run(10)
    return outcome, judge(spec, outcome)


def test_naive_local_time_with_timezone(tmp_path: Path) -> None:
    make_db(tmp_path, "2026-11-11 09:00:00")
    # 09:00 in Brussels (UTC+1 in November) is 08:00Z: 2.5 h before NOW
    outcome, core = run(spec_for(tmp_path, 'timezone = "Europe/Brussels"'))
    assert isinstance(outcome, Reading)
    assert (core.value, core.unit) == (2.5, "h")
    assert core.summary == "newest entry 2.5 h ago"
    assert outcome.fields["newest"] == "2026-11-11 09:00"  # in the configured zone


def test_naive_time_is_utc_without_timezone(tmp_path: Path) -> None:
    make_db(tmp_path, "2026-11-11 09:00:00")
    assert run(spec_for(tmp_path))[1].value == 1.5


@pytest.mark.parametrize(
    "stored",
    [
        "2026-11-11T09:00:00Z",
        "2026-11-11T10:00:00+01:00",
        "2026-11-11 09:00:00+00:00",
        1794387600,
        "1794387600",
        1794387600.0,
    ],
)
def test_rfc3339_and_unix_seconds(tmp_path: Path, stored: object) -> None:
    assert datetime(2026, 11, 11, 9, 0, tzinfo=UTC).timestamp() == 1794387600
    make_db(tmp_path, stored)
    assert run(spec_for(tmp_path))[1].value == 1.5


def test_limits_on_durations(tmp_path: Path) -> None:
    make_db(tmp_path, "2026-11-08 10:30:00")  # 3 days
    _, core = run(spec_for(tmp_path, 'warn = "26h"\ncrit = "50h"'))
    assert (core.level, core.value, core.unit) == ("crit", 3.0, "d")


@pytest.mark.parametrize("stored", [None, ""])
def test_null_or_empty_is_crit_by_default(tmp_path: Path, stored: object) -> None:
    make_db(tmp_path, stored)
    outcome, core = run(spec_for(tmp_path))
    assert outcome == Observation(level="crit", summary="no rows")
    assert core.level == "crit"


def test_no_rows_and_the_empty_option(tmp_path: Path) -> None:
    make_db(tmp_path)
    q = "SELECT finished_at FROM runs"
    assert run(spec_for(tmp_path, query=q))[1].level == "crit"
    for level in ("ok", "warn", "unknown"):
        assert run(spec_for(tmp_path, f'empty = "{level}"', query=q))[1].level == level
    with pytest.raises(ConfigError, match="'empty' must be one of"):
        spec_for(tmp_path, 'empty = "red"')


def test_summary_template_with_age_and_newest(tmp_path: Path) -> None:
    make_db(tmp_path, "2026-11-11 05:30:00")
    spec = spec_for(tmp_path, 'summary = "last backup {age} ago ({newest})"')
    assert run(spec)[1].summary == "last backup 5.0 h ago (2026-11-11 05:30)"


def test_more_than_one_column_is_a_failure(tmp_path: Path) -> None:
    make_db(tmp_path, "2026-11-11 09:00:00")
    with pytest.raises(CheckFailure, match="one column"):
        run(spec_for(tmp_path, query="SELECT finished_at, ok FROM runs"))


def test_a_value_that_is_not_a_timestamp_is_a_failure(tmp_path: Path) -> None:
    make_db(tmp_path, "yesterday-ish")
    outcome = execute(spec_for(tmp_path))
    assert outcome.summary.startswith("the query's value is not a timestamp")  # type: ignore[union-attr]


def test_failures_have_short_summaries(tmp_path: Path) -> None:
    make_db(tmp_path, "2026-11-11 09:00:00")
    outcome = execute(spec_for(tmp_path, query="SELECT x FROM nosuchtable"))
    assert outcome.summary == "error: OperationalError"  # type: ignore[union-attr]
    (tmp_path / "log.db").unlink()
    assert execute(spec_for(tmp_path)).summary == "cannot open the database"  # type: ignore[union-attr]


def test_sqlite_is_opened_read_only(tmp_path: Path) -> None:
    path = make_db(tmp_path, "2026-11-11 09:00:00")
    target = parse_database_url("sqlite:log.db", None, tmp_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        fetch_first_row(target, "INSERT INTO runs VALUES ('x', 1)", 5)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


# URLs and extras


def test_password_in_the_url_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="password_env"):
        parse_database_url("postgresql://monitor:hunter2@db.example/backups", None, tmp_path)


def test_sqlite_paths(tmp_path: Path) -> None:
    assert parse_database_url("sqlite:///var/lib/x.db", None, tmp_path).path == Path("/var/lib/x.db")
    assert parse_database_url("sqlite:rel/x.db", None, tmp_path).path == tmp_path / "rel/x.db"
    assert parse_database_url("sqlite:///C:/data/x.db", None, tmp_path).path == Path("C:/data/x.db")
    with pytest.raises(ValueError, match="must start with"):
        parse_database_url("oracle://x/y", None, tmp_path)


@pytest.mark.parametrize(
    ("url", "extra"),
    [
        ("mysql://m@db.example/b", "mysql"),
        ("mariadb://m@db.example/b", "mysql"),
        ("postgresql://m@db.example/b", "postgres"),
    ],
)
def test_missing_extra_is_a_config_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, extra: str
) -> None:
    monkeypatch.setitem(sys.modules, "pymysql", None)
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ValueError, match=rf"\[{extra}\]"):
        parse_database_url(url, None, tmp_path)
    text = f'[[check]]\nid = "d"\ntype = "sql_age"\ndatabase = "{url}"\nquery = "select 1"\n'
    with pytest.raises(ConfigError, match=rf"mmm-anystatus-checker\[{extra}\]"):
        parse(text, tmp_path)


def test_server_urls_are_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pymysql", types.ModuleType("pymysql"))
    monkeypatch.setitem(sys.modules, "psycopg", types.ModuleType("psycopg"))
    my = parse_database_url("mariadb://monitor@db.example:3307/backups", "pw", tmp_path)
    assert (my.kind, my.host, my.port, my.user, my.name, my.password) == (
        "mysql",
        "db.example",
        3307,
        "monitor",
        "backups",
        "pw",
    )
    pg = parse_database_url("postgresql://monitor@db.example/backups", None, tmp_path)
    assert (pg.kind, pg.host, pg.port, pg.user, pg.name) == (
        "postgres",
        "db.example",
        None,
        "monitor",
        "backups",
    )
    with pytest.raises(ValueError, match="password_env"):
        parse_database_url("sqlite:x.db", "pw", tmp_path)
