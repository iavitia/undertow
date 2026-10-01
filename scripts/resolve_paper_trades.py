"""Settles any 'open' paper_trades row whose instrument has expired, using
the same expiry-settlement estimate validated in api/main.py's
compute_wallet_positions and scripts/backtest_copy_trades.py's
simulate_copy_trades -- nearest available index price (any wallet, same
asset) to the expiry timestamp, standing in for the real settlement price
Derive's API doesn't expose.

v1 simplification (stated in the plan, not an oversight): every paper
position is held to expiry rather than mirroring the source wallet's own
subsequent closing trades. Whether to follow their exits too is a real
design question deferred until there's enough resolved-trade data to
reason about it.

Naked-short loss floor (see conversation, replaces the retired live
poll-triggered stop-loss engine): a sell-side position's return_pct is
clamped at config.NAKED_SHORT_LOSS_FLOOR_PCT (-100%) -- the same max-loss
shape as a bought option, modeling a margined position getting liquidated
the instant its posted margin (the premium collected) is exhausted, rather
than a live poll trying to catch that exact moment. A buy's loss is
already naturally bounded at -100% (exit_price can't go below 0), so the
floor only ever changes anything for sells.
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.pnl import intrinsic_value, nearest_index_price
from config import DB_PATH, NAKED_SHORT_LOSS_FLOOR_PCT, SETTLEMENT_FRESHNESS_TOLERANCE_MS


def resolve(conn):
    now_ms = int(time.time() * 1000)
    open_rows = conn.execute(
        "SELECT * FROM paper_trades WHERE fill_status = 'open' AND expiry * 1000 <= ?", (now_ms,)
    ).fetchall()

    resolved = 0
    for p in open_rows:
        expiry_ms = p["expiry"] * 1000
        spot, gap_ms = nearest_index_price(conn, p["asset"], expiry_ms)
        if spot is None:
            continue  # no market data yet at this asset near expiry -- retry next run
        if gap_ms > SETTLEMENT_FRESHNESS_TOLERANCE_MS:
            continue  # nearest sample is too far from the real expiry moment to trust yet -- retry next run
        exit_price = intrinsic_value(spot, p["strike"], p["option_type"])

        if p["side"] == "buy":
            return_pct = (exit_price - p["entry_price"]) / p["entry_price"]
            loss_capped = 0
        else:
            return_pct = (p["entry_price"] - exit_price) / p["entry_price"]
            loss_capped = 1 if return_pct < NAKED_SHORT_LOSS_FLOOR_PCT else 0
            return_pct = max(return_pct, NAKED_SHORT_LOSS_FLOOR_PCT)

        conn.execute(
            """
            UPDATE paper_trades
            SET fill_status = 'resolved', resolved_ts = ?, exit_price = ?, return_pct = ?,
                pnl_is_estimated = 1, loss_capped = ?
            WHERE id = ?
            """,
            (expiry_ms, exit_price, return_pct, loss_capped, p["id"]),
        )
        resolved += 1

    conn.commit()
    return resolved


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    n = resolve(conn)
    print(f"resolved {n} paper trade(s)")
    conn.close()


if __name__ == "__main__":
    run()
