from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from helpers import parse

from anystatus.checks.sql_value import SqlValue
from anystatus.judge import judge
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.scheduler import execute
from anystatus.table import ConfigError


def make_db(tmp_path: Path, *values: object) -> None:
    conn = sqlite3.connect(tmp_path / "queue.db")
    conn.execute("CREATE TABLE outgoing (n, sent_at)")
    conn.executemany("INSERT INTO outgoing VALUES (?, NULL)", [(v,) for v in values])
    conn.commit()
    conn.close()


def spec_for(tmp_path: Path, extra: str = "", query: str = "SELECT count(*) FROM outgoing WHERE sent_at IS NULL"):  # type: ignore[no-untyped-def]
    text = f'[[check]]\nid = "queue"\ntype = "sql_value"\ndatabase = "sqlite:queue.db"\nquery = "{query}"\n{extra}\n'
    return parse(text, tmp_path).checks[0]


def run(spec):  # type: ignore[no-untyped-def]
    outcome = spec.check.run(10)
    return outcome, judge(spec, outcome)


def test_count_with_limits_higher_is_worse(tmp_path: Path) -> None:
    make_db(tmp_path, *range(25))
    spec = spec_for(tmp_path, "warn = 20\ncrit = 200")
    assert spec.limits.worse == "above"
    outcome, core = run(spec)
    assert outcome == Reading(value=25, text="25")
    assert (core.level, core.value, core.summary) == ("warn", 25, "25")
    make_db_more = sqlite3.connect(tmp_path / "queue.db")
    make_db_more.executemany("INSERT INTO outgoing VALUES (?, NULL)", [(i,) for i in range(200)])
    make_db_more.commit()
    make_db_more.close()
    assert run(spec)[1].level == "crit"


def test_worse_below_overrides_the_direction(tmp_path: Path) -> None:
    make_db(tmp_path, 1, 2)
    spec = spec_for(tmp_path, 'worse = "below"\nwarn = 5\ncrit = 1')
    assert run(spec)[1].level == "warn"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 12.5", 12.5),
        ("SELECT '7'", 7.0),  # text that holds a number
        ("SELECT -3", -3.0),
        ("SELECT 1e3", 1000.0),
    ],
)
def test_number_forms(tmp_path: Path, sql: str, expected: float) -> None:
    make_db(tmp_path)
    assert run(spec_for(tmp_path, query=sql))[0] == Reading(value=expected, text=f"{expected:g}")


def test_decimal_from_a_driver_is_a_number(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from anystatus.checks import sql_value

    monkeypatch.setattr(sql_value, "fetch_first_row", lambda target, query, timeout: (Decimal("12.50"),))
    make_db(tmp_path)
    assert run(spec_for(tmp_path))[0] == Reading(value=12.5, text="12.5")


def test_null_and_no_rows_are_crit_by_default_and_empty_changes_it(tmp_path: Path) -> None:
    make_db(tmp_path)
    for query in ("SELECT max(n) FROM outgoing", "SELECT n FROM outgoing"):  # NULL, and no row at all
        assert run(spec_for(tmp_path, query=query))[0] == Observation(level="crit", summary="no rows")
        for level in ("ok", "warn", "unknown"):
            assert run(spec_for(tmp_path, f'empty = "{level}"', query=query))[1].level == level
    with pytest.raises(ConfigError, match="'empty' must be one of"):
        spec_for(tmp_path, 'empty = "red"')


def test_a_value_that_is_not_a_number_is_a_failure(tmp_path: Path) -> None:
    make_db(tmp_path)
    with pytest.raises(CheckFailure, match="not a number"):
        run(spec_for(tmp_path, query="SELECT 'hunter2'"))
    outcome = execute(spec_for(tmp_path, query="SELECT 'hunter2'"))
    assert outcome.summary == "the query's value is not a number"  # type: ignore[union-attr]
    assert execute(spec_for(tmp_path, query="SELECT x'00'")).summary == "the query's value is not a number"  # type: ignore[union-attr]


def test_more_than_one_column_is_a_failure(tmp_path: Path) -> None:
    make_db(tmp_path, 1)
    with pytest.raises(CheckFailure, match="one column, it returned 2"):
        run(spec_for(tmp_path, query="SELECT n, sent_at FROM outgoing"))


def test_summary_template(tmp_path: Path) -> None:
    make_db(tmp_path, 1, 2, 3)
    assert run(spec_for(tmp_path, 'summary = "{value} mails waiting"'))[1].summary == "3 mails waiting"


def test_database_failures_have_short_summaries(tmp_path: Path) -> None:
    make_db(tmp_path)
    assert execute(spec_for(tmp_path, query="SELECT x FROM nosuchtable")).summary == "error: OperationalError"  # type: ignore[union-attr]
    (tmp_path / "queue.db").unlink()
    assert execute(spec_for(tmp_path)).summary == "cannot open the database"  # type: ignore[union-attr]


def test_sqlite_is_read_only(tmp_path: Path) -> None:
    make_db(tmp_path, 1)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        run(spec_for(tmp_path, query="INSERT INTO outgoing VALUES (9, NULL)"))


def test_same_database_rules_as_sql_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = 'id = "q"\ntype = "sql_value"\nquery = "select 1"\n'
    with pytest.raises(ConfigError, match="password_env"):
        parse(f'[[check]]\n{base}database = "postgresql://monitor:hunter2@db.example/x"\n', tmp_path)
    with pytest.raises(ConfigError, match="'password_env' does not apply to SQLite"):
        parse(f'[[check]]\n{base}database = "sqlite:x.db"\npassword_env = "PW"\n', tmp_path, {"PW": "x"})
    with pytest.raises(ConfigError, match="must start with"):
        parse(f'[[check]]\n{base}database = "oracle://x/y"\n', tmp_path)
    with pytest.raises(ConfigError, match="missing 'database'"):
        parse(f"[[check]]\n{base}", tmp_path)
    with pytest.raises(ConfigError, match="missing 'query'"):
        parse('[[check]]\nid = "q"\ntype = "sql_value"\ndatabase = "sqlite:x.db"\n', tmp_path)
    with pytest.raises(ConfigError, match="'query' must be a non-empty string"):
        parse('[[check]]\nid = "q"\ntype = "sql_value"\ndatabase = "sqlite:x.db"\nquery = 5\n', tmp_path)
    with pytest.raises(ConfigError, match="unknown key 'timezone'"):  # a time zone is for sql_age
        parse(f'[[check]]\n{base}database = "sqlite:x.db"\ntimezone = "UTC"\n', tmp_path)
    monkeypatch.setitem(__import__("sys").modules, "pymysql", None)
    with pytest.raises(ConfigError, match=r"mmm-anystatus-checker\[mysql\]"):
        parse(f'[[check]]\n{base}database = "mariadb://m@db.example/b"\n', tmp_path)
    ok = parse(f'[[check]]\n{base}database = "sqlite:x.db"\n', tmp_path).checks[0].check
    assert isinstance(ok, SqlValue)
