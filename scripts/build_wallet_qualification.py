"""Batch-computes wallet_qualification for every wallet with enough copy-
eligible history -- see analysis/qualification.py for the methodology.
Replaces scripts/backtest_walkforward.py + scripts/build_copy_candidates.py
(both superseded, see their tables' comments in db/schema.sql).

Reuses the exact same validated simulate_copy_trades() positions as
scripts/backtest_copy_trades.py -- one position simulation, not a second
one that could drift out of sync."""
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.qualification import qualify_wallet, risk_discipline_score
from config import DB_PATH
from scripts.backtest_copy_trades import load_events, simulate_copy_trades


def compute_risk_inputs(conn):
    """tail_ratio, rfq_pct, sizing_cv per wallet -- the context behind
    risk_discipline_score. tail_ratio and worst-position data come from
    copy_trade_backtest (already computed); rfq_pct and sizing_cv need a
    fresh aggregation over raw options_events, since nothing else tracks
    per-wallet notional variance or RFQ share."""
    tail_rows = conn.execute(
        "SELECT wallet_address, copy_total_return_pct, copy_worst_return_pct FROM copy_trade_backtest"
    ).fetchall()
    tail_ratios = {}
    for r in tail_rows:
        worst = r["copy_worst_return_pct"]
        tail_ratios[r["wallet_address"]] = (r["copy_total_return_pct"] / abs(worst)) if worst not in (None, 0) else None

    df = pd.read_sql_query(
        "SELECT wallet_address, notional_usd, rfq_id FROM options_events WHERE wallet_address IS NOT NULL",
        conn,
    )
    df["is_rfq"] = df["rfq_id"].notna()
    agg = df.groupby("wallet_address").agg(
        rfq_pct=("is_rfq", "mean"),
        notional_mean=("notional_usd", "mean"),
        notional_std=("notional_usd", "std"),
    )
    agg["sizing_cv"] = (agg["notional_std"] / agg["notional_mean"]).fillna(0)

    rfq_pcts = agg["rfq_pct"].to_dict()
    sizing_cvs = agg["sizing_cv"].to_dict()
    return tail_ratios, rfq_pcts, sizing_cvs


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    t0 = time.time()
    df = load_events(conn)
    print(f"loaded {len(df)} rows in {time.time()-t0:.1f}s")

    now_ms = int(time.time() * 1000)
    t0 = time.time()
    positions, _, _ = simulate_copy_trades(df, now_ms)
    print(f"simulated {len(positions)} copy trades in {time.time()-t0:.1f}s")

    by_wallet = {}
    for p in positions:
        by_wallet.setdefault(p["wallet_address"], []).append(p)

    bot_map = {r["wallet_address"]: bool(r["is_likely_bot"]) for r in conn.execute("SELECT wallet_address, is_likely_bot FROM wallet_leaderboard")}
    tail_ratios, rfq_pcts, sizing_cvs = compute_risk_inputs(conn)

    conn.execute("DELETE FROM wallet_qualification")
    now_ms2 = int(time.time() * 1000)
    rows_out = []
    tier_counts = {}

    for wallet, wallet_positions in by_wallet.items():
        wallet_positions.sort(key=lambda p: p["resolved_ts"])
        returns = [p["return_pct"] for p in wallet_positions]
        result = qualify_wallet(returns)
        tier_counts[result["tier"]] = tier_counts.get(result["tier"], 0) + 1

        pre, fwd = result["pre"], result["forward"]
        qualified_at_ts = wallet_positions[result["qualified_at_n"] - 1]["resolved_ts"] if result["qualified_at_n"] else None
        risk_score = risk_discipline_score(tail_ratios.get(wallet), rfq_pcts.get(wallet), sizing_cvs.get(wallet))

        rows_out.append((
            wallet, result["total_n"], result["tier"], result["qualified_at_n"], qualified_at_ts,
            pre["n"] if pre else None, pre["win_rate"] if pre else None, pre["wilson_low"] if pre else None,
            pre["total_return_pct"] if pre else None,
            fwd["n"] if fwd else None, fwd["win_rate"] if fwd else None, fwd["wilson_low"] if fwd else None,
            fwd["total_return_pct"] if fwd else None, fwd["median_return_pct"] if fwd else None,
            risk_score, rfq_pcts.get(wallet), sizing_cvs.get(wallet),
            int(bot_map.get(wallet, False)), now_ms2,
        ))

    conn.executemany(
        """
        INSERT INTO wallet_qualification
            (wallet_address, total_n, tier, qualified_at_n, qualified_at_ts,
             pre_qualification_n, pre_qualification_win_rate, pre_qualification_wilson_low, pre_qualification_total_return_pct,
             forward_n, forward_win_rate, forward_wilson_low, forward_total_return_pct, forward_median_return_pct,
             risk_discipline_score, rfq_pct, sizing_cv, is_likely_bot, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_out,
    )
    # Append-only history alongside the current-state table above (see
    # conversation -- wallet_qualification itself is DELETE+reinserted
    # every run, so it can only ever show "now," never a trend). Same rows,
    # same shape, just INSERT instead of DELETE+INSERT -- one snapshot per
    # wallet per real (non-skipped) requalification pass.
    conn.executemany(
        """
        INSERT INTO wallet_qualification_history
            (wallet_address, total_n, tier, qualified_at_n, qualified_at_ts,
             pre_qualification_n, pre_qualification_win_rate, pre_qualification_wilson_low, pre_qualification_total_return_pct,
             forward_n, forward_win_rate, forward_wilson_low, forward_total_return_pct, forward_median_return_pct,
             risk_discipline_score, rfq_pct, sizing_cv, is_likely_bot, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_out,
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    print(f"wrote {len(rows_out)} wallet_qualification rows (+{len(rows_out)} history)")
    print("tier breakdown:", tier_counts)
    conn.close()


if __name__ == "__main__":
    run()
