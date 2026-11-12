"""The checker's memory: one SQLite file, one row per check, only ever touched by the one checker process."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from anystatus.model import LEVELS, Core, Level, Reported, Streaks
from anystatus.timeutil import format_time, parse_time

log = logging.getLogger("anystatus")

SCHEMA_VERSION = 2
_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id TEXT PRIMARY KEY,
    result TEXT NOT NULL DEFAULT '',  -- the last reported result, JSON as served ('' until the first run)
    since TEXT NOT NULL DEFAULT '',   -- when the reported level started
    warn_since TEXT,               -- the streaks show_after measures: since when runs saw warn or worse,
    crit_since TEXT,               -- crit,
    unknown_since TEXT,            -- and failures (NULL: no such streak)
    updated TEXT NOT NULL,         -- when this row was written
    extra TEXT,                    -- JSON the check type keeps for itself (e.g. mqtt: last message time and payload)
    extra_updated TEXT             -- when extra was last written
)
"""
_KEPT = "id, result, since, updated, extra, extra_updated"  # what a version 1 file (confirm streaks) hands over


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        # Check types with a listener thread (mqtt) write their extra from that thread: one lock, one connection.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.conn:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(checks)")}
            if columns and "warn_since" not in columns:
                # Version 1 counted confirm runs. Results, since and the types' own data carry over; a pending
                # confirm streak can't become a show_after streak, and is dropped.
                self.conn.execute("ALTER TABLE checks RENAME TO checks_v1")
                self.conn.execute(_SCHEMA)
                self.conn.execute(f"INSERT INTO checks ({_KEPT}) SELECT {_KEPT} FROM checks_v1")
                self.conn.execute("DROP TABLE checks_v1")
                # The shown level has lasted since `since`: its streaks start there, so a card doesn't change.
                for check_id, result, since in self.conn.execute("SELECT id, result, since FROM checks").fetchall():
                    try:
                        level = json.loads(result)["level"]
                    except (ValueError, KeyError, TypeError):
                        continue
                    self.conn.execute(
                        "UPDATE checks SET warn_since = ?, crit_since = ?, unknown_since = ? WHERE id = ?",
                        (
                            since if level in ("warn", "crit") else None,
                            since if level == "crit" else None,
                            since if level == "unknown" else None,
                            check_id,
                        ),
                    )
            self.conn.execute(_SCHEMA)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    def load_all(self) -> dict[str, Reported]:
        with self._lock:
            rows = self.conn.execute("SELECT id, result, since, warn_since, crit_since, unknown_since FROM checks WHERE result != ''").fetchall()
        out: dict[str, Reported] = {}
        for check_id, result, since, *streaks in rows:
            try:
                out[check_id] = _decode(result, since, streaks)
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("%s: ignoring an unreadable stored row (%s)", check_id, type(exc).__name__)
        return out

    def save(self, check_id: str, result_json: str, reported: Reported, updated: datetime) -> None:
        """Write the row for one check, in one transaction."""
        s = reported.streaks
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO checks (id, result, since, warn_since, crit_since, unknown_since, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET result=excluded.result, since=excluded.since,"
                " warn_since=excluded.warn_since, crit_since=excluded.crit_since,"
                " unknown_since=excluded.unknown_since, updated=excluded.updated",
                (
                    check_id,
                    result_json,
                    format_time(reported.since),
                    *(format_time(t) if t else None for t in (s.warn, s.crit, s.unknown)),
                    format_time(updated),
                ),
            )

    def prune(self, keep: Collection[str]) -> list[str]:
        """Delete the rows of checks that are no longer configured. Returns their ids."""
        with self._lock:
            existing = [r[0] for r in self.conn.execute("SELECT id FROM checks")]
        gone = [i for i in existing if i not in set(keep)]
        with self._lock, self.conn:
            self.conn.executemany("DELETE FROM checks WHERE id = ?", [(i,) for i in gone])
        return gone

    def load_extra(self) -> dict[str, tuple[dict[str, Any], datetime]]:
        """The type-specific data of every check, with when it was written."""
        with self._lock:
            rows = self.conn.execute("SELECT id, extra, extra_updated FROM checks WHERE extra IS NOT NULL").fetchall()
        out: dict[str, tuple[dict[str, Any], datetime]] = {}
        for check_id, extra, written in rows:
            try:
                data = json.loads(extra)
                if isinstance(data, dict):
                    out[check_id] = (data, parse_time(written))
            except (ValueError, TypeError):
                log.warning("%s: ignoring unreadable stored data", check_id)
        return out

    def save_extra(self, check_id: str, data: dict[str, Any], written: datetime) -> None:
        """Write the type-specific data of one check; works before the check's first result exists."""
        stamp = format_time(written)
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO checks (id, updated, extra, extra_updated) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET extra=excluded.extra, extra_updated=excluded.extra_updated",
                (check_id, stamp, json.dumps(data), stamp),
            )


def _decode(result: str, since: str, streaks: list[str | None]) -> Reported:
    doc: dict[str, Any] = json.loads(result)
    level = doc["level"]
    if level not in LEVELS:
        raise ValueError("level")
    core = Core(
        level=cast(Level, level),
        summary=doc.get("summary"),
        value=doc.get("value"),
        unit=doc.get("unit"),
    )
    warn, crit, unknown = (parse_time(t) if t else None for t in streaks)
    return Reported(
        core=core,
        ts=parse_time(doc["ts"]).astimezone(UTC),
        since=parse_time(since),
        streaks=Streaks(warn, crit, unknown),
    )
