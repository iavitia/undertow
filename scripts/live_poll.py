"""Incremental ingest of new Derive trades since the last run, tracked via
ingest_state.last_ts. This is PROJECT_PLAN.md's originally-scoped "Phase 5 --
Live Polling," never actually built (only the manual, full-range
scripts/backfill_derive.py existed) -- finishing it here because
scripts/run_watchlist_agent.py needs a live feed as a dependency anyway, and
it's the same feed the anomaly detectors would use for the same purpose.

Reuses backfill_derive.py's upsert_trade() rather than reimplementing
insert/upsert logic -- same table, same conflict-resolution rules, no
reason for a second copy to exist and drift.

Run standalone (`python scripts/live_poll.py`) or import ingest() from
scripts/run_watchlist_agent.py."""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.derive_client import iter_trade_history
from config import BACKFILL_START_ISO, DB_PATH
from scripts.backfill_derive import upsert_trade

CURRENCIES = ("ETH", "BTC", "SOL")
INGEST_SOURCE = "derive_live"


def _seed_from_ts(conn):
    """First run, no ingest_state row yet: start from the latest trade
    already in the DB rather than BACKFILL_START_ISO, so this doesn't
    re-walk the entire historical range the manual backfill already
    covered."""
    row = conn.execute("SELECT MAX(timestamp) AS ts FROM options_events").fetchone()
    if row["ts"] is not None:
        return row["ts"]
    from datetime import datetime, timezone
    return int(
        datetime.strptime(BACKFILL_START_ISO, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
    )


def ingest(conn):
    """Pulls and upserts every new trade since the last run. Returns the
    number of trade legs inserted/updated. Commits internally (per-page,
    same pattern as backfill_derive.py) and updates ingest_state.last_ts
    at the end -- if this raises partway through, last_ts is NOT advanced,
    so the next run safely re-covers the same window rather than skipping
    trades that failed to ingest."""
    row = conn.execute("SELECT last_ts FROM ingest_state WHERE source = ?", (INGEST_SOURCE,)).fetchone()
    from_ts = row["last_ts"] if row else _seed_from_ts(conn)
    to_ts = int(time.time() * 1000)

    if from_ts >= to_ts:
        return 0

    total = 0
    for currency in CURRENCIES:
        for trades, page, num_pages in iter_trade_history(currency, from_ts, to_ts):
            for trade in trades:
                if upsert_trade(conn, currency, trade):
                    total += 1
            conn.commit()

    conn.execute(
        """
        INSERT INTO ingest_state (source, last_ts) VALUES (?, ?)
        ON CONFLICT (source) DO UPDATE SET last_ts = excluded.last_ts
        """,
        (INGEST_SOURCE, to_ts),
    )
    conn.commit()
    return total


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    n = ingest(conn)
    print(f"ingested {n} trade leg(s)")
    conn.close()


if __name__ == "__main__":
    run()
