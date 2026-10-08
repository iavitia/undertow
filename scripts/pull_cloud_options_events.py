"""Weekly step 1 of 2 (see scripts/prune_cloud_options_events.py for step
2): pulls every row cloud's options_events has accumulated since this
project moved the live path to GitHub Actions/Supabase, and upserts them
into the local archive -- the thing that actually makes the "ingest
market-wide while the laptop's off, then catch up for wallet discovery
later" design (see conversation) work. Cloud is the only place that's
seen trades since local's own ingest stopped (the Windows scheduled
tasks were retired in favor of this project's GitHub Actions workflows).

Pulls everything every run rather than tracking a cursor -- cloud's
options_events is small (10-30k rows, tens of MB) specifically because
scripts/prune_cloud_options_events.py keeps it that way, so a full pull
is cheap and avoids a second piece of cursor state to get wrong. Safe to
run as often as you like: the same (source, source_trade_id,
wallet_address) UNIQUE constraint and upsert shape
scripts/backfill_derive.py's upsert_trade() already uses makes this
idempotent.

Doesn't touch any of local's anomaly-detection columns (trade size/
volume/skew z-scores, wallet-jump flags) -- cloud's options_events is
deliberately trimmed to not have them (see db/cloud_schema.sql), and the
UPDATE clause below only ever touches the same handful of columns
upsert_trade() does, leaving anything local already computed alone."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DB_PATH
from db.cloud_conn import get_cloud_conn

PAGE_SIZE = 5000


def upsert_row(local_conn, row):
    local_conn.execute(
        """
        INSERT INTO options_events
            (source, asset, instrument, strike, expiry, option_type, side,
             size, price, notional_usd, index_price_usd, wallet_address,
             tx_hash, source_trade_id, timestamp,
             mark_price, rfq_id, tx_status, trade_fee, liquidity_role, realized_pnl, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, source_trade_id, wallet_address) DO UPDATE SET
            mark_price = excluded.mark_price,
            rfq_id = excluded.rfq_id,
            tx_status = excluded.tx_status,
            trade_fee = excluded.trade_fee,
            liquidity_role = excluded.liquidity_role,
            realized_pnl = excluded.realized_pnl,
            raw_json = excluded.raw_json
        """,
        (
            row["source"], row["asset"], row["instrument"], row["strike"], row["expiry"], row["option_type"],
            row["side"], row["size"], row["price"], row["notional_usd"], row["index_price_usd"],
            row["wallet_address"], row["tx_hash"], row["source_trade_id"], row["timestamp"],
            row["mark_price"], row["rfq_id"], row["tx_status"], row["trade_fee"], row["liquidity_role"],
            row["realized_pnl"], row["raw_json"],
        ),
    )


def pull(local_conn, cloud_conn):
    total = 0
    offset = 0
    while True:
        page = cloud_conn.execute(
            "SELECT * FROM options_events ORDER BY id LIMIT ? OFFSET ?",
            (PAGE_SIZE, offset),
        ).fetchall()
        if not page:
            break
        for row in page:
            upsert_row(local_conn, row)
        local_conn.commit()
        total += len(page)
        print(f"  pulled {total} rows so far...")
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return total


def run():
    local_conn = sqlite3.connect(DB_PATH)
    local_conn.row_factory = sqlite3.Row
    cloud_conn = get_cloud_conn()

    n = pull(local_conn, cloud_conn)

    local_conn.close()
    cloud_conn.close()
    print(f"done -- upserted {n} rows from cloud into local")


if __name__ == "__main__":
    run()
