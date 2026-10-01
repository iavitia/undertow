import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.deribit_client import get_recent_trades, parse_instrument
from config import DB_PATH

CURRENCY = "BTC"

# Deribit's public trade endpoint only retains ~24-48h of history (see
# PROJECT_PLAN.md section 3), so on first run this is as far back as we can
# ever go for BTC -- there is no separate backfill step.
FALLBACK_LOOKBACK_MS = 47 * 3600 * 1000


def last_seen_timestamp(conn):
    row = conn.execute(
        "SELECT MAX(timestamp) FROM options_events WHERE source = 'deribit' AND asset = ?",
        (CURRENCY,),
    ).fetchone()
    return row[0]


def insert_trade(conn, trade):
    parsed = parse_instrument(trade["instrument_name"])
    if parsed is None:
        print(f"skipping unparseable instrument: {trade['instrument_name']}")
        return False

    index_price = float(trade["index_price"])
    size = float(trade["amount"])

    conn.execute(
        """
        INSERT OR IGNORE INTO options_events
            (source, asset, instrument, strike, expiry, option_type, side,
             size, price, notional_usd, index_price_usd, source_trade_id,
             timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "deribit",
            CURRENCY,
            trade["instrument_name"],
            parsed["strike"],
            parsed["expiry"],
            parsed["option_type"],
            trade["direction"],
            size,
            trade["price"],
            size * index_price,
            index_price,
            trade["trade_id"],
            trade["timestamp"],
        ),
    )
    return True


def run():
    conn = sqlite3.connect(DB_PATH)
    now_ms = int(time.time() * 1000)

    last_ts = last_seen_timestamp(conn)
    start_ts = (last_ts + 1) if last_ts else (now_ms - FALLBACK_LOOKBACK_MS)

    total = 0
    inserted = 0
    while True:
        trades, has_more = get_recent_trades(CURRENCY, start_ts, now_ms, count=1000)
        for trade in trades:
            total += 1
            if insert_trade(conn, trade):
                inserted += 1
        conn.commit()
        if not trades or not has_more:
            break
        start_ts = trades[-1]["timestamp"] + 1

    print(f"Done. {total} trades fetched, {inserted} parsed and upserted.")
    conn.close()


if __name__ == "__main__":
    run()
