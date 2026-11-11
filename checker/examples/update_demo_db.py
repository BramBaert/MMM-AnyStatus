"""Creates and updates examples/demo.sqlite, the database that examples/config.toml checks. Standard library only.

    uv run examples/update_demo_db.py          # write one heartbeat, then exit
    uv run examples/update_demo_db.py --loop   # keep writing one every 20 seconds (Ctrl+C to stop)

`heartbeat.written_at` is an RFC 3339 time in UTC; `nightly_runs.finished_at` is a plain local-looking time without an
offset, 3 days old, so the "Nightly job" check is red. Stop the loop (or don't start it) and the heartbeat check goes
amber after a minute and red after five: that is what a stuck job looks like.
"""

import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

DB = Path(__file__).with_name("demo.sqlite")


def write_once() -> None:
    now = datetime.now(UTC)
    conn = sqlite3.connect(DB)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS heartbeat (written_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS nightly_runs (finished_at TEXT, ok INTEGER)")
        conn.execute("DELETE FROM heartbeat")
        conn.execute("INSERT INTO heartbeat VALUES (?)", (now.strftime("%Y-%m-%dT%H:%M:%SZ"),))
        if conn.execute("SELECT count(*) FROM nightly_runs").fetchone()[0] == 0:
            stale = (now - timedelta(days=3)).replace(hour=2, minute=15, second=0, microsecond=0)
            conn.execute("INSERT INTO nightly_runs VALUES (?, 1)", (stale.strftime("%Y-%m-%d %H:%M:%S"),))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    write_once()
    print(f"wrote a heartbeat to {DB}")
    while "--loop" in sys.argv:
        time.sleep(20)
        write_once()
