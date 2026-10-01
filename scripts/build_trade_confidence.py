"""Builds wallet_edge_profile: per-wallet copy-trade performance broken out
by (asset, option_type, entry_side) instead of one overall number.

The idea (see conversation, originally from a discussion about the general
"copying wallets" problem): a wallet's edge is usually concentrated in a
specific kind of trade, not uniform across everything it does -- a wallet
proven at selling ETH puts isn't necessarily proven at buying BTC calls,
even if its overall win rate looks great. scripts/run_watchlist_agent.py
records each new paper trade's bucket stats at detection time
(paper_trades.edge_bucket_*) so we can check later whether that
distinction actually predicts anything -- this is deliberately NOT used to
filter/reject trades yet (see conversation: no evidence yet that it's
predictive, and filtering now would slow down collecting the outcome data
needed to check).

Reuses the exact same validated simulate_copy_trades() positions as
scripts/backtest_copy_trades.py (now carrying asset/option_type per
position) rather than recomputing anything -- one position simulation,
multiple views of it."""
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.qualification import buy_edge_lower_bound
from config import DB_PATH
from scripts.backtest_copy_trades import load_events, simulate_copy_trades, wilson_lower_bound

MIN_BUCKET_N = 5  # below this, wilson_low already reflects the uncertainty, but the row would be pure noise
# Higher floor for the pooled buy-side bucket: buy_edge_lower_bound's
# skewness term is itself noisy under ~15-20 samples (see its docstring
# and analysis/qualification.py's MIN_BUY_BUCKET_N), so a bucket this
# thin isn't written at all rather than written with an unreliable stat.
MIN_BUY_BUCKET_N = 20


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    t0 = time.time()
    df = load_events(conn)
    print(f"loaded {len(df)} rows in {time.time()-t0:.1f}s")

    now_ms = int(time.time() * 1000)
    positions, _, _ = simulate_copy_trades(df, now_ms)
    print(f"simulated {len(positions)} copy trades")

    buckets = {}
    buy_pooled = {}  # (wallet, asset) -> returns, call+put pooled, buy-side only
    for p in positions:
        key = (p["wallet_address"], p["asset"], p["option_type"], p["entry_side"])
        buckets.setdefault(key, []).append(p["return_pct"])
        if p["entry_side"] == "buy":
            buy_pooled.setdefault((p["wallet_address"], p["asset"]), []).append(p["return_pct"])

    conn.execute("DELETE FROM wallet_edge_profile")
    now_ms2 = int(time.time() * 1000)
    rows_out = []
    for (wallet, asset, option_type, side), returns in buckets.items():
        n = len(returns)
        if n < MIN_BUCKET_N:
            continue
        wins = sum(1 for r in returns if r > 0)
        rows_out.append((
            wallet, asset, option_type, side, n,
            wins / n, wilson_lower_bound(wins, n),
            float(np.median(returns)), float(np.mean(returns)), None, None, now_ms2,
        ))

    n_pooled = 0
    for (wallet, asset), returns in buy_pooled.items():
        n = len(returns)
        if n < MIN_BUY_BUCKET_N:
            continue
        wins = sum(1 for r in returns if r > 0)
        lcb, top1_share, _ = buy_edge_lower_bound(returns)
        rows_out.append((
            wallet, asset, "ALL", "buy", n,
            wins / n, wilson_lower_bound(wins, n),
            float(np.median(returns)), float(np.mean(returns)), lcb, top1_share, now_ms2,
        ))
        n_pooled += 1

    conn.executemany(
        """
        INSERT INTO wallet_edge_profile
            (wallet_address, asset, option_type, entry_side, n, win_rate, wilson_low,
             median_return_pct, avg_return_pct, return_lcb, top1_gain_share, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_out,
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    print(f"wrote {len(rows_out)} wallet_edge_profile rows (buckets with n>={MIN_BUCKET_N}, "
          f"including {n_pooled} pooled buy-side buckets with n>={MIN_BUY_BUCKET_N})")
    conn.close()


if __name__ == "__main__":
    run()
