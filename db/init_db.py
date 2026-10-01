import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH, SCHEMA_PATH


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())
    # without this, the query planner can pick a stale plan for new indexes
    # (seen firsthand: a partial index on is_flagged was ignored in favor of
    # a full scan until stats were refreshed -- 3s vs 6ms on the same query)
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    print(f"Initialized DB at {DB_PATH}")


if __name__ == "__main__":
    init_db()
