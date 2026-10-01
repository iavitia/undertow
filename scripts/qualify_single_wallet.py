"""On-demand qualification for a single wallet -- same
analysis/qualification.py logic as scripts/build_wallet_qualification.py's
batch run, just scoped to one wallet so it can run synchronously when that
wallet is manually added to the watchlist (see api/main.py's POST
/api/watchlist) or on request: `python scripts/qualify_single_wallet.py <address>`.

Replaces scripts/walkforward_single_wallet.py, which used the older
fixed-midpoint-split methodology now superseded by the no-look-ahead
qualification walk."""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.qualification import qualify_wallet, risk_discipline_score
from config import DB_PATH
from scripts.backtest_copy_trades import load_events, simulate_copy_trades
from scripts.build_wallet_qualification import compute_risk_inputs


def run_for_wallet(conn, wallet_address, df=None):
    """Computes and upserts a wallet_qualification row for one wallet.
    Returns the qualify_wallet() result dict."""
    if df is None:
        df = load_events(conn)
    now_ms = int(time.time() * 1000)
    positions, _, _ = simulate_copy_trades(df, now_ms)
    wallet_positions = sorted(
        (p for p in positions if p["wallet_address"] == wallet_address),
        key=lambda p: p["resolved_ts"],
    )
    returns = [p["return_pct"] for p in wallet_positions]
    result = qualify_wallet(returns)

    tail_ratios, rfq_pcts, sizing_cvs = compute_risk_inputs(conn)
    risk_score = risk_discipline_score(tail_ratios.get(wallet_address), rfq_pcts.get(wallet_address), sizing_cvs.get(wallet_address))
    bot_row = conn.execute("SELECT is_likely_bot FROM wallet_leaderboard WHERE wallet_address = ?", (wallet_address,)).fetchone()
    is_bot = bool(bot_row["is_likely_bot"]) if bot_row else False

    pre, fwd = result["pre"], result["forward"]
    qualified_at_ts = wallet_positions[result["qualified_at_n"] - 1]["resolved_ts"] if result["qualified_at_n"] else None
    now_ms2 = int(time.time() * 1000)

    conn.execute(
        """
        INSERT INTO wallet_qualification
            (wallet_address, total_n, tier, qualified_at_n, qualified_at_ts,
             pre_qualification_n, pre_qualification_win_rate, pre_qualification_wilson_low, pre_qualification_total_return_pct,
             forward_n, forward_win_rate, forward_wilson_low, forward_total_return_pct, forward_median_return_pct,
             risk_discipline_score, rfq_pct, sizing_cv, is_likely_bot, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (wallet_address) DO UPDATE SET
            total_n=excluded.total_n, tier=excluded.tier, qualified_at_n=excluded.qualified_at_n,
            qualified_at_ts=excluded.qualified_at_ts,
            pre_qualification_n=excluded.pre_qualification_n, pre_qualification_win_rate=excluded.pre_qualification_win_rate,
            pre_qualification_wilson_low=excluded.pre_qualification_wilson_low,
            pre_qualification_total_return_pct=excluded.pre_qualification_total_return_pct,
            forward_n=excluded.forward_n, forward_win_rate=excluded.forward_win_rate,
            forward_wilson_low=excluded.forward_wilson_low, forward_total_return_pct=excluded.forward_total_return_pct,
            forward_median_return_pct=excluded.forward_median_return_pct,
            risk_discipline_score=excluded.risk_discipline_score, rfq_pct=excluded.rfq_pct, sizing_cv=excluded.sizing_cv,
            is_likely_bot=excluded.is_likely_bot, computed_at=excluded.computed_at
        """,
        (
            wallet_address, result["total_n"], result["tier"], result["qualified_at_n"], qualified_at_ts,
            pre["n"] if pre else None, pre["win_rate"] if pre else None, pre["wilson_low"] if pre else None,
            pre["total_return_pct"] if pre else None,
            fwd["n"] if fwd else None, fwd["win_rate"] if fwd else None, fwd["wilson_low"] if fwd else None,
            fwd["total_return_pct"] if fwd else None, fwd["median_return_pct"] if fwd else None,
            risk_score, rfq_pcts.get(wallet_address), sizing_cvs.get(wallet_address), int(is_bot), now_ms2,
        ),
    )
    conn.commit()
    return result


def run(wallet_address):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 8000")
    result = run_for_wallet(conn, wallet_address)
    print(f"tier: {result['tier']}  total_n: {result['total_n']}  qualified_at: {result['qualified_at_n']}")
    if result["forward"]:
        f = result["forward"]
        print(f"forward: n={f['n']} win_rate={f['win_rate']*100:.1f}% wilson_low={f['wilson_low']*100:.1f}% total_return={f['total_return_pct']*100:.1f}%")
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/qualify_single_wallet.py <wallet_address>")
        sys.exit(1)
    run(sys.argv[1])
