import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.derive_client import iter_trade_history, parse_instrument
from config import BACKFILL_START_ISO, DB_PATH

CURRENCIES = ("ETH", "BTC", "SOL")


def upsert_trade(conn, currency, trade):
    parsed = parse_instrument(trade["instrument_name"])
    if parsed is None:
        print(f"skipping unparseable instrument: {trade['instrument_name']}")
        return False

    index_price = float(trade["index_price"])
    size = float(trade["trade_amount"])

    conn.execute(
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
            "derive",
            currency,
            trade["instrument_name"],
            parsed["strike"],
            parsed["expiry"],
            parsed["option_type"],
            trade["direction"],
            size,
            float(trade["trade_price"]),
            size * index_price,
            index_price,
            trade["wallet"],
            trade["tx_hash"],
            trade["trade_id"],
            trade["timestamp"],
            float(trade["mark_price"]) if trade.get("mark_price") is not None else None,
            trade.get("rfq_id"),
            trade.get("tx_status"),
            float(trade["trade_fee"]) if trade.get("trade_fee") is not None else None,
            trade.get("liquidity_role"),
            float(trade["realized_pnl"]) if trade.get("realized_pnl") is not None else None,
            json.dumps(trade),
        ),
    )

    conn.execute(
        """
        INSERT INTO wallets (address, chain, first_seen)
        VALUES (?, 'derive-l2', ?)
        ON CONFLICT (address) DO UPDATE SET
            -- CASE, not MIN(a,b): SQLite's MIN doubles as a 2-arg scalar
            -- "least of" function, but Postgres's MIN is aggregate-only
            -- (needs LEAST(), which SQLite lacks) -- CASE is the one form
            -- both engines accept identically, since this function is
            -- shared between the local-only full backfill (SQLite) and
            -- the cloud live-path ingest (Postgres, see db/cloud_conn.py).
            first_seen = CASE WHEN wallets.first_seen < excluded.first_seen THEN wallets.first_seen ELSE excluded.first_seen END
        """,
        (trade["wallet"], trade["timestamp"]),
    )
    return True


def run():
    conn = sqlite3.connect(DB_PATH)
    # scripts/run_watchlist_agent.py's scheduled task can be writing to the
    # same DB every ~15 min while this runs (see conversation -- a backfill
    # crashed partway through with "database is locked" for exactly this
    # reason); wait briefly on a lock instead of failing instantly, same
    # pattern already used there.
    conn.execute("PRAGMA busy_timeout = 8000")
    to_ts = int(time.time() * 1000)
    from_ts = int(
        datetime.strptime(BACKFILL_START_ISO, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )

    for currency in CURRENCIES:
        print(f"=== {currency} ===")
        total = 0
        inserted = 0
        for trades, page, num_pages in iter_trade_history(currency, from_ts, to_ts):
            for trade in trades:
                total += 1
                if upsert_trade(conn, currency, trade):
                    inserted += 1
            conn.commit()
            print(f"{currency} page {page}/{num_pages} — {total} trades processed so far")

        print(f"{currency} done. {total} trade legs processed, {inserted} parsed and upserted.")

    conn.close()


if __name__ == "__main__":
    run()
