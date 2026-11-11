"""Creates and updates examples/demo.sqlite, the database that examples/config.toml checks, and demo_result.json, a result
file that the `result_file` check reads. Standard library only.

    uv run examples/update_demo_db.py          # write one heartbeat, then exit
    uv run examples/update_demo_db.py --loop   # keep writing one every 20 seconds (Ctrl+C to stop)

`heartbeat.written_at` is an RFC 3339 time in UTC; `nightly_runs.finished_at` is a plain local-looking time without an
offset, 3 days old, so the "Nightly job" check is red. Stop the loop (or don't start it) and the heartbeat check goes
amber after a minute and red after five: that is what a stuck job looks like.
"""

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

DB = Path(__file__).with_name("demo.sqlite")
# what a job in any language would write, for the `result_file` check:
RESULT = Path(__file__).with_name("demo_result.json")


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
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # A single SPEC result (SPEC.md). ts is when the job ran; ttl says how long it may be believed: when this script
    # stops, the card turns grey after 90 s. Written to a temporary file and renamed, so a reader never sees half of it.
    result = {
        "level": "ok",
        "summary": "demo job finished",
        "value": 42,
        "unit": "rows",
        "since": stamp,
        "ts": stamp,
        "ttl": 90,
    }
    tmp = RESULT.with_name(RESULT.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    tmp.replace(RESULT)


if __name__ == "__main__":
    write_once()
    print(f"wrote a heartbeat to {DB} and a result to {RESULT}")
    while "--loop" in sys.argv:
        time.sleep(20)
        write_once()
