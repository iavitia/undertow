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

Naked shorts settle UNCAPPED (see docs/quant-review-2026-10.md): the
-100%-of-premium loss floor this used to apply (config's retired
NAKED_SHORT_LOSS_FLOOR_PCT) assumed a short is liquidated once the premium
collected is exhausted, but real short-option margin is a multiple of the
premium, so losses run well past -100%. loss_capped is now always written
as 0; it stays set only on historical rows (see
scripts/repair_uncap_paper_trade_losses.py).

Fees: mode='simulated' rows are charged the same copier taker fee on entry
that scripts/backtest_copy_trades.py charges (analysis.pnl.copy_taker_fee_pct,
priced off the index price nearest entry_ts), so paper results stay
comparable to the backtest that selected the wallet. Expiry settlement has
no taker leg, so no exit fee. testnet_order rows are left gross -- they
carry real fills, and their real fees aren't tracked per row.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.pnl import copy_taker_fee_pct, intrinsic_value, nearest_index_price
from config import SETTLEMENT_FRESHNESS_TOLERANCE_MS
from db.cloud_conn import get_live_conn


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
        else:
            return_pct = (p["entry_price"] - exit_price) / p["entry_price"]

        if p["mode"] == "simulated":
            entry_index, _ = nearest_index_price(conn, p["asset"], p["entry_ts"])
            if entry_index is not None:
                return_pct -= copy_taker_fee_pct(p["entry_price"], entry_index, p["entry_price"])

        conn.execute(
            """
            UPDATE paper_trades
            SET fill_status = 'resolved', resolved_ts = ?, exit_price = ?, return_pct = ?,
                pnl_is_estimated = 1, loss_capped = 0
            WHERE id = ?
            """,
            (expiry_ms, exit_price, return_pct, p["id"]),
        )
        resolved += 1

    conn.commit()
    return resolved


def resolve_mainnet_shadows(conn):
    """Settles each row's mainnet-shadow estimate (shadow_* columns, written
    by run_live_agent._mainnet_shadow_entry for configs with
    "mainnet_shadow") once its instrument has expired: same expiry
    settlement as resolve() above, net of the mainnet fee charged at entry.

    Deliberately independent of fill_status: a testnet order that never
    filled on testnet's thin book still gets a shadow result, because on
    mainnet a taker order at the bid/ask would have filled -- whether the
    trade happens shouldn't depend on testnet liquidity. A row whose
    testnet stop fired is skipped: run_live_agent._record_stop_exit gives
    its shadow a matching exit at that moment instead."""
    now_ms = int(time.time() * 1000)
    rows = conn.execute(
        """
        SELECT * FROM paper_trades
        WHERE shadow_entry_price IS NOT NULL AND shadow_return_pct IS NULL AND expiry * 1000 <= ?
          AND fill_status <> 'stopped_out'
        """,
        (now_ms,),
    ).fetchall()

    resolved = 0
    for p in rows:
        spot, gap_ms = nearest_index_price(conn, p["asset"], p["expiry"] * 1000)
        if spot is None or gap_ms > SETTLEMENT_FRESHNESS_TOLERANCE_MS:
            continue  # same retry-next-run rule as resolve()
        exit_price = intrinsic_value(spot, p["strike"], p["option_type"])
        entry = p["shadow_entry_price"]
        gross = (exit_price - entry) if p["side"] == "buy" else (entry - exit_price)
        shadow_return_pct = (gross - p["shadow_fee_per_unit"]) / entry
        conn.execute(
            "UPDATE paper_trades SET shadow_exit_price = ?, shadow_return_pct = ? WHERE id = ?",
            (exit_price, shadow_return_pct, p["id"]),
        )
        resolved += 1

    conn.commit()
    return resolved


def run():
    conn = get_live_conn()
    n = resolve(conn)
    print(f"resolved {n} paper trade(s)")
    conn.close()


if __name__ == "__main__":
    run()
