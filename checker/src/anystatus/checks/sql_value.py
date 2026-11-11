"""`sql_value`: a number that a query returns ("how many mails are stuck in the queue?"). Higher is worse by default.

The database rules (URL, `password_env`, read-only, drivers) are those of `sql_age`.
"""

from __future__ import annotations

from anystatus.checks.base import Check, register
from anystatus.checks.database import DatabaseTarget, fetch_first_row, target_from_table
from anystatus.jsonpath import to_number
from anystatus.model import CheckFailure, Observation, Reading
from anystatus.table import Table


@register
class SqlValue(Check):
    type_name = "sql_value"

    def __init__(self, target: DatabaseTarget, query: str, empty: str) -> None:
        self.target = target
        self.query = query
        self.empty = empty

    @classmethod
    def from_config(cls, table: Table) -> SqlValue:
        target = target_from_table(table)
        query = table.req_str("query")
        empty = table.choice("empty", ("crit", "ok", "warn", "unknown"), "crit")
        return cls(target, query, empty)

    def run(self, timeout: float) -> Observation | Reading:
        row = fetch_first_row(self.target, self.query, timeout)
        if row is not None and len(row) != 1:
            raise CheckFailure(f"the query must return one column, it returned {len(row)}")
        value = None if row is None else row[0]
        if value is None or (isinstance(value, str) and not value.strip()):
            return Observation(level=self.empty, summary="no rows")
        try:
            number = to_number(value)
        except ValueError as exc:
            raise CheckFailure("the query's value is not a number") from exc
        return Reading(value=number, text=f"{number:g}")
