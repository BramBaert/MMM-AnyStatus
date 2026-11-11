"""`sql_age`: the age of a timestamp that a query returns ("when did the last backup finish?").

This is where the classic trap lives: MariaDB DATETIME and SQLite text are usually *local* time, and a time without an
offset is read as UTC unless `timezone` says otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

from anystatus.checks.base import Check, register
from anystatus.checks.database import DatabaseTarget, fetch_first_row, parse_database_url
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.table import Table
from anystatus.timeutil import age_seconds, format_duration, parse_timestamp


def _now() -> datetime:
    return datetime.now(UTC)


@register
class SqlAge(Check):
    type_name = "sql_age"
    duration_limits = True
    placeholders = frozenset({"newest"})

    def __init__(self, target: DatabaseTarget, query: str, zone: tzinfo, empty: str) -> None:
        self.target = target
        self.query = query
        self.zone = zone
        self.empty = empty

    @classmethod
    def from_config(cls, table: Table) -> SqlAge:
        url = table.req_str("database")
        query = table.req_str("query")
        zone = table.timezone("timezone")
        empty = table.choice("empty", ("crit", "ok", "warn", "unknown"), "crit")
        password = table.env("password")
        target = DatabaseTarget("sqlite")
        if url:
            try:
                target = parse_database_url(url, password, table.base_dir)
            except ValueError as exc:
                table.error(str(exc))
        return cls(target, query, zone, empty)

    def run(self, timeout: float) -> Observation | Reading:
        row = fetch_first_row(self.target, self.query, timeout)
        if row is not None and len(row) != 1:
            raise CheckFailure(f"the query must return one column, it returned {len(row)}")
        value = None if row is None else row[0]
        if value is None or (isinstance(value, str) and not value.strip()):
            return Observation(level=self.empty, summary="no rows")
        try:
            newest = parse_timestamp(value, self.zone)
        except ValueError as exc:
            raise CheckFailure(f"the query's value is not a timestamp ({exc})") from exc
        seconds = age_seconds(newest, _now())
        shown, unit = format_duration(seconds)
        return Reading(
            value=seconds,
            seconds=True,
            text=f"newest entry {shown:.1f} {unit} ago",
            fields={"newest": newest.astimezone(self.zone).strftime("%Y-%m-%d %H:%M")},
        )
