import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH


def fmt(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def run():
    conn = sqlite3.connect(DB_PATH)

    for source, asset in (("derive", "ETH"), ("derive", "BTC"), ("deribit", "BTC")):
        row = conn.execute(
            "SELECT COUNT(*), MIN(timestamp), MAX(timestamp), COUNT(DISTINCT wallet_address) "
            "FROM options_events WHERE source = ? AND asset = ?",
            (source, asset),
        ).fetchone()
        count, min_ts, max_ts, wallets = row
        print(f"\n=== {source} / {asset} ===")
        if count == 0:
            print("No rows found.")
            continue
        print(f"rows: {count}")
        print(f"date range: {fmt(min_ts)} to {fmt(max_ts)}")
        if wallets:
            print(f"distinct wallets: {wallets}")

        # per-day counts, flag any day with zero trades within the observed range
        # (a real gap, not just "before backfill started")
        rows = conn.execute(
            """
            SELECT strftime('%Y-%m-%d', timestamp / 1000, 'unixepoch') AS day, COUNT(*)
            FROM options_events WHERE source = ? AND asset = ?
            GROUP BY day ORDER BY day
            """,
            (source, asset),
        ).fetchall()
        counts = [c for _, c in rows]
        if counts:
            avg = sum(counts) / len(counts)
            low_days = [(d, c) for d, c in rows if c < avg * 0.2]
            print(f"days with data: {len(rows)}, avg trades/day: {avg:.1f}")
            if low_days:
                print(f"days with <20% of average volume ({len(low_days)}): {low_days[:10]}")

    conn.close()


if __name__ == "__main__":
    run()
