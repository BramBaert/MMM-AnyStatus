"""sql_age and sql_value against a real MariaDB and a real PostgreSQL. Skipped unless a server is named, and needs the
extras (`uv run --extra mysql --extra postgres pytest tests/test_real_databases.py`):

    ANYSTATUS_TEST_MARIADB=host:port     a MariaDB (or MySQL) server
    ANYSTATUS_TEST_POSTGRES=host:port    a PostgreSQL server
    ANYSTATUS_TEST_DB_PASSWORD=...       the password of user `anystatus` on database `anystatus_test` (on both)

for example (PowerShell; the throwaway password goes through the environment, never into the command line):

    $env:ANYSTATUS_TEST_DB_PASSWORD = "some-fake-password"
    $env:MARIADB_ROOT_PASSWORD = $env:MARIADB_PASSWORD = $env:POSTGRES_PASSWORD = $env:ANYSTATUS_TEST_DB_PASSWORD
    docker run -d --rm -p 43306:3306 -e MARIADB_ROOT_PASSWORD -e MARIADB_PASSWORD -e MARIADB_USER=anystatus `
        -e MARIADB_DATABASE=anystatus_test mariadb:11.4
    docker run -d --rm -p 45432:5432 -e POSTGRES_PASSWORD -e POSTGRES_USER=anystatus `
        -e POSTGRES_DB=anystatus_test postgres:18-alpine
    $env:ANYSTATUS_TEST_MARIADB = "localhost:43306"; $env:ANYSTATUS_TEST_POSTGRES = "localhost:45432"

The tests create (and drop) a table of their own with the same account, then read it through the checks, which
connect read-only.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from helpers import parse

from anystatus.checks import sql_age
from anystatus.checks.database import fetch_first_row, parse_database_url
from anystatus.config import CheckSpec
from anystatus.judge import judge
from anystatus.model import Failure, Observation, Reading
from anystatus.scheduler import execute

NOW = datetime(2026, 11, 11, 10, 30, tzinfo=UTC)
PASSWORD = os.environ.get("ANYSTATUS_TEST_DB_PASSWORD", "")
USER, DATABASE = "anystatus", "anystatus_test"


@dataclass(frozen=True)
class Flavour:
    name: str
    variable: str
    scheme: str
    create: str  # the table, with the same columns on both
    read_only_error: str  # part of the server's message when a write is refused


MARIADB = Flavour(
    "mariadb",
    "ANYSTATUS_TEST_MARIADB",
    "mariadb",
    "CREATE TABLE {t} (finished_at DATETIME NULL, stamp TIMESTAMP NULL, unix_s BIGINT NULL, "
    "amount DECIMAL(10,2) NULL, label VARCHAR(20) NULL)",
    "READ ONLY",
)
POSTGRES = Flavour(
    "postgres",
    "ANYSTATUS_TEST_POSTGRES",
    "postgresql",
    "CREATE TABLE {t} (finished_at timestamp NULL, stamp timestamptz NULL, unix_s bigint NULL, "
    "amount numeric(10,2) NULL, label varchar(20) NULL)",
    "read-only",
)
FLAVOURS = [
    pytest.param(
        f,
        id=f.name,
        marks=pytest.mark.skipif(
            not (os.environ.get(f.variable) and PASSWORD),
            reason=f"set {f.variable}=host:port and ANYSTATUS_TEST_DB_PASSWORD to run against a real server",
        ),
    )
    for f in (MARIADB, POSTGRES)
]


class Server:
    def __init__(self, flavour: Flavour, tmp_path: Path) -> None:
        self.flavour = flavour
        self.tmp_path = tmp_path
        self.host, _, port = os.environ[flavour.variable].partition(":")
        self.port = int(port)
        self.table = f"t_{uuid.uuid4().hex[:10]}"
        self.conn: Any = None

    def connect(self) -> Any:
        if self.flavour is MARIADB:
            import pymysql

            return pymysql.connect(
                host=self.host,
                port=self.port,
                user=USER,
                password=PASSWORD,
                database=DATABASE,
                autocommit=True,
            )
        import psycopg

        return psycopg.connect(
            host=self.host, port=self.port, user=USER, password=PASSWORD, dbname=DATABASE, autocommit=True
        )

    def sql(self, statement: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(statement.format(t=self.table))

    def count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {self.table}")
            return int(cur.fetchone()[0])

    @property
    def url(self) -> str:
        return f"{self.flavour.scheme}://{USER}@{self.host}:{self.port}/{DATABASE}"

    def spec(
        self, kind: str, query: str, extra: str = "", password: str = PASSWORD, url: str = ""
    ) -> CheckSpec:
        query = query.format(t=self.table).replace('"', '\\"')
        text = f"""
[[check]]
id = "db"
type = "{kind}"
database = "{url or self.url}"
password_env = "DB_PW"
query = "{query}"
{extra}
"""
        return parse(text, self.tmp_path, {"DB_PW": password}).checks[0]


@pytest.fixture
def server(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Server]:
    monkeypatch.setattr(sql_age, "_now", lambda: NOW)
    srv = Server(request.param, tmp_path)
    srv.conn = srv.connect()
    srv.sql(srv.flavour.create)
    srv.sql(
        "INSERT INTO {t} (finished_at, stamp, unix_s, amount, label) "
        "VALUES ('2026-11-11 09:00:00', '2026-11-11 09:00:00', 1794387600, 12.50, 'abc')"
    )
    try:
        yield srv
    finally:
        srv.sql("DROP TABLE {t}")
        srv.conn.close()


def on_both(test: Callable[..., None]) -> Callable[..., None]:
    return pytest.mark.parametrize("server", FLAVOURS, indirect=True)(test)


def run(spec: CheckSpec) -> tuple[Any, Any]:
    outcome = spec.check.run(10)
    return outcome, judge(spec, outcome)


# sql_age


@on_both
def test_naive_datetime_is_utc_unless_timezone_says_otherwise(server: Server) -> None:
    query = "SELECT max(finished_at) FROM {t}"
    assert run(server.spec("sql_age", query))[1].value == 1.5  # 09:00 read as UTC
    outcome, core = run(
        server.spec("sql_age", query, 'timezone = "Europe/Brussels"')
    )  # 09:00 Brussels = 08:00Z
    assert isinstance(outcome, Reading)
    assert (core.value, core.unit, core.summary) == (2.5, "h", "newest entry 2.5 h ago")
    assert outcome.fields["newest"] == "2026-11-11 09:00"


@on_both
def test_other_timestamp_columns(server: Server) -> None:
    # TIMESTAMP (MariaDB) / timestamptz (PostgreSQL), written without an offset in the server's UTC session
    assert run(server.spec("sql_age", "SELECT max(stamp) FROM {t}"))[1].value == 1.5
    assert (
        run(server.spec("sql_age", "SELECT max(unix_s) FROM {t}"))[1].value == 1.5
    )  # Unix seconds in a bigint
    assert run(server.spec("sql_age", "SELECT '2026-11-11T10:00:00+01:00'"))[1].value == 1.5  # RFC 3339 text


@on_both
def test_age_limits_and_summary(server: Server) -> None:
    extra = 'warn = "1h"\ncrit = "2h"\nsummary = "last run {age} ago ({newest})"'
    core = run(server.spec("sql_age", "SELECT max(finished_at) FROM {t}", extra))[1]
    assert (core.level, core.summary) == ("warn", "last run 1.5 h ago (2026-11-11 09:00)")


@on_both
def test_no_row_and_null_are_crit_unless_empty_says_otherwise(server: Server) -> None:
    server.sql("DELETE FROM {t}")
    for query in ("SELECT finished_at FROM {t}", "SELECT max(finished_at) FROM {t}"):  # no row, NULL
        assert run(server.spec("sql_age", query))[0] == Observation(level="crit", summary="no rows")
        assert run(server.spec("sql_age", query, 'empty = "ok"'))[1].level == "ok"


# sql_value


@on_both
def test_sql_value_numbers(server: Server) -> None:
    assert run(server.spec("sql_value", "SELECT count(*) FROM {t}"))[0] == Reading(value=1, text="1")
    outcome, core = run(server.spec("sql_value", "SELECT max(amount) FROM {t}", "warn = 10\ncrit = 20"))
    assert outcome == Reading(value=12.5, text="12.5")  # a DECIMAL / numeric comes back as Decimal
    assert core.level == "warn"
    assert run(server.spec("sql_value", "SELECT max(unix_s) FROM {t}"))[0].value == 1794387600
    assert run(server.spec("sql_value", "SELECT 2.5e0 * 2"))[0].value == 5.0


@on_both
def test_sql_value_empty_and_wrong_shapes(server: Server) -> None:
    assert run(server.spec("sql_value", "SELECT sum(amount) FROM {t} WHERE 1 = 0"))[0] == Observation(
        level="crit", summary="no rows"
    )
    assert (
        run(server.spec("sql_value", "SELECT amount FROM {t} WHERE 1 = 0", 'empty = "warn"'))[1].level
        == "warn"
    )
    outcome = execute(server.spec("sql_value", "SELECT label FROM {t}"))
    assert outcome == Failure("the query's value is not a number")
    outcome = execute(server.spec("sql_value", "SELECT amount, label FROM {t}"))
    assert outcome == Failure("the query must return one column, it returned 2")


# the connection


@on_both
def test_the_connection_is_read_only(server: Server) -> None:
    target = parse_database_url(server.url, PASSWORD, server.tmp_path)
    with pytest.raises(Exception, match=f"(?i){server.flavour.read_only_error}"):
        fetch_first_row(target, f"INSERT INTO {server.table} (label) VALUES ('x')", 10)
    assert server.count() == 1


@on_both
def test_wrong_password_is_a_short_failure_without_the_password(server: Server) -> None:
    wrong = "not-the-password-" + PASSWORD
    spec = server.spec("sql_value", "SELECT 1", password=wrong)
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and judge(spec, outcome).level == "unknown"
    assert wrong not in outcome.summary and PASSWORD not in outcome.summary
    assert len(outcome.summary) < 40


@on_both
def test_refused_connection_is_a_failure(server: Server) -> None:
    # one address, short timeout: libpq on Windows waits out the whole connect_timeout, per address, for a refused port
    spec = server.spec(
        "sql_value",
        "SELECT 1",
        'timeout = "2s"',
        url=f"{server.flavour.scheme}://{USER}@127.0.0.1:1/{DATABASE}",
    )
    outcome = execute(spec)
    assert isinstance(outcome, Failure) and PASSWORD not in outcome.summary


@on_both
def test_a_slow_query_gives_up_at_the_timeout(server: Server) -> None:
    sleep = "SELECT sleep(6)" if server.flavour is MARIADB else "SELECT pg_sleep(6)"
    spec = server.spec("sql_value", sleep, 'timeout = "1s"')
    started = time.monotonic()
    outcome = execute(spec)
    assert time.monotonic() - started < 4
    assert isinstance(outcome, Failure) and "timed out" in outcome.summary


@on_both
def test_error_in_the_query_is_a_failure(server: Server) -> None:
    outcome = execute(server.spec("sql_value", "SELECT x FROM nosuchtable"))
    assert isinstance(outcome, Failure) and outcome.summary in (
        "ProgrammingError 1146",
        "error: UndefinedTable",
    )
