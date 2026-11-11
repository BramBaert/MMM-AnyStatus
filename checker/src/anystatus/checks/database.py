"""Database access for the `sql_*` check types: parse a database URL, open a connection per run, fetch one row.

SQLite is in the standard library. MariaDB/MySQL (PyMySQL) and PostgreSQL (psycopg) are optional extras, imported only
when a check needs them.
"""

from __future__ import annotations

import importlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote, urlparse

from anystatus.model import CheckFailure
from anystatus.table import Table

_SCHEMES = {
    "sqlite": "sqlite",
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgresql": "postgres",
    "postgres": "postgres",
}
_DRIVERS = {"mysql": ("pymysql", "mysql"), "postgres": ("psycopg", "postgres")}


@dataclass(frozen=True)
class DatabaseTarget:
    kind: str  # "sqlite", "mysql" or "postgres"
    path: Path | None = None  # sqlite
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    name: str | None = None


def load_driver(kind: str) -> ModuleType:
    """The driver module of an extra; ValueError naming the extra to install when it is missing."""
    module_name, extra = _DRIVERS[kind]
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(
            f"this database needs the '{extra}' extra, which is not installed: "
            f'uv add "mmm-anystatus-checker[{extra}]"'
        ) from exc


def parse_database_url(url: str, password: str | None, base_dir: Path) -> DatabaseTarget:
    """Read a `database` URL. Raises ValueError with the config problem.

    sqlite:///var/lib/x.db is the absolute path /var/lib/x.db (as in file:///); sqlite:x.db or sqlite:./x.db is
    relative to the config file. `password` comes from `password_env`, never from the URL.
    """
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("'database' is not a valid URL") from exc
    kind = _SCHEMES.get(scheme)
    if kind is None:
        raise ValueError(
            f"'database' must start with sqlite:, mysql://, mariadb:// or postgresql://, not '{scheme or url}'"
        )
    if parsed.password is not None:
        raise ValueError(
            "a password in the database URL is not allowed: use 'password_env' to name an environment variable"
        )
    if kind == "sqlite":
        if password is not None:
            raise ValueError("'password_env' does not apply to SQLite")
        raw = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            raw = unquote(parsed.netloc + parsed.path)  # sqlite://relative.db
        if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]  # sqlite:///C:/path
        if not raw:
            raise ValueError("'database' has no file path")
        path = Path(raw)
        if not path.is_absolute() and not raw.startswith("/"):
            path = base_dir / path
        return DatabaseTarget("sqlite", path=path)
    if not parsed.hostname:
        raise ValueError("'database' has no host name")
    name = unquote(parsed.path.lstrip("/")) or None
    load_driver(kind)
    return DatabaseTarget(
        kind,
        host=parsed.hostname,
        port=port,
        user=unquote(parsed.username) if parsed.username else None,
        password=password,
        name=name,
    )


def target_from_table(table: Table) -> DatabaseTarget:
    """The `database` and `password_env` keys of a check, as a target (a config problem is recorded on the table)."""
    url = table.req_str("database")
    password = table.env("password")
    if url:
        try:
            return parse_database_url(url, password, table.base_dir)
        except ValueError as exc:
            table.error(str(exc))
    return DatabaseTarget("sqlite")


def fetch_first_row(target: DatabaseTarget, query: str, timeout: float) -> tuple[Any, ...] | None:
    """Open a connection, run the query, return its first row (None when there is none), close again."""
    seconds = max(1, int(timeout))
    if target.kind == "sqlite":
        assert target.path is not None
        uri = target.path.resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=timeout)
        except sqlite3.Error as exc:
            raise CheckFailure("cannot open the database", detail=str(exc)) from exc
        try:
            row = conn.execute(query).fetchone()
        finally:
            conn.close()
        return None if row is None else tuple(row)
    driver = load_driver(target.kind)
    try:
        if target.kind == "mysql":
            return _fetch_mysql(driver, target, query, seconds)
        return _fetch_postgres(driver, target, query, seconds)
    except Exception as exc:
        if _is_timeout(target.kind, exc):
            raise CheckFailure(
                f"query timed out after {seconds} s", detail=f"{type(exc).__name__}: {exc}"
            ) from exc
        raise


# What the servers and drivers say when a query takes too long: MariaDB's max_statement_time (1969), MySQL's
# max_execution_time (3024) and PyMySQL's own read timeout (2013, "lost connection"); PostgreSQL's QueryCanceled.
_MYSQL_TIMEOUT_CODES = (1969, 3024, 2013)


def _is_timeout(kind: str, exc: Exception) -> bool:
    if kind == "mysql":
        return bool(exc.args) and exc.args[0] in _MYSQL_TIMEOUT_CODES
    return type(exc).__name__ == "QueryCanceled"


def _fetch_mysql(
    driver: ModuleType, target: DatabaseTarget, query: str, seconds: int
) -> tuple[Any, ...] | None:
    conn = driver.connect(
        host=target.host,
        port=target.port or 3306,
        user=target.user,
        password=target.password or "",
        database=target.name,
        connect_timeout=seconds,
        read_timeout=seconds + 1,  # the server's own limit (below) should speak first
        write_timeout=seconds,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            # Stop the query on the server too, not just stop waiting for it: MariaDB (seconds), MySQL (ms).
            for statement in (
                f"SET SESSION max_statement_time = {seconds}",
                f"SET SESSION max_execution_time = {seconds * 1000}",
            ):
                try:
                    cur.execute(statement)
                    break
                except driver.MySQLError:
                    continue
            cur.execute(query)
            row = cur.fetchone()
    finally:
        conn.close()
    return None if row is None else tuple(row)


def _fetch_postgres(
    driver: ModuleType, target: DatabaseTarget, query: str, seconds: int
) -> tuple[Any, ...] | None:
    conn = driver.connect(
        host=target.host,
        port=target.port or 5432,
        user=target.user,
        password=target.password,
        dbname=target.name,
        connect_timeout=seconds,
        options=f"-c default_transaction_read_only=on -c statement_timeout={seconds * 1000}",
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone()
    finally:
        conn.close()
    return None if row is None else tuple(row)
