"""On-demand walk-forward validation for a single wallet, split at that
wallet's OWN median resolved-trade timestamp rather than the whole
dataset's fixed cutoff (scripts/backtest_walkforward.py's batch run).

Needed because a wallet that started trading after the dataset-wide
cutoff has zero formation-period data under the batch script's one global
split date -- that's a timing artifact, not a quality signal (see
conversation: drift-vault-68 looked clean on every other check -- 84.5%
Wilson, zero RFQ trades, consistent position sizing -- but started
trading months after the global cutoff, so the batch walk-forward run
could never evaluate it). This runs the identical walk-forward logic
(same period_stats/wilson_lower_bound), just anchored to the wallet's own
timeline.

Called automatically whenever a wallet is manually added to the watchlist
(see api/main.py's POST /api/watchlist) and standalone for any wallet on
request: `python scripts/walkforward_single_wallet.py <address>`."""
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH
from scripts.backtest_copy_trades import load_events, simulate_copy_trades, wilson_lower_bound

MIN_PER_HALF = 10


def period_stats(returns):
    n = len(returns)
    wins = sum(1 for r in returns if r > 0)
    return {
        "n": n,
        "win_rate": wins / n,
        "wilson_low": wilson_lower_bound(wins, n),
        "median_return_pct": float(np.median(returns)),
        "avg_return_pct": float(np.mean(returns)),
    }


def run_for_wallet(conn, wallet_address, df=None):
    """Computes and upserts a walkforward_backtest row for one wallet,
    split at ITS OWN median resolved_ts. Returns {"formation": stats,
    "validation": stats, "cutoff_ts": ts} or None if there isn't enough
    data yet (< MIN_PER_HALF*2 resolved copy positions total)."""
    if df is None:
        df = load_events(conn)
    now_ms = int(time.time() * 1000)
    positions, _, _ = simulate_copy_trades(df, now_ms)
    wallet_positions = [p for p in positions if p["wallet_address"] == wallet_address]

    if len(wallet_positions) < MIN_PER_HALF * 2:
        return None

    wallet_positions.sort(key=lambda p: p["resolved_ts"])
    mid = len(wallet_positions) // 2
    formation_returns = [p["return_pct"] for p in wallet_positions[:mid]]
    validation_returns = [p["return_pct"] for p in wallet_positions[mid:]]

    if len(formation_returns) < MIN_PER_HALF or len(validation_returns) < MIN_PER_HALF:
        return None

    f = period_stats(formation_returns)
    v = period_stats(validation_returns)
    cutoff_ts = wallet_positions[mid]["resolved_ts"]
    now_ms2 = int(time.time() * 1000)

    conn.execute(
        """
        INSERT INTO walkforward_backtest
            (wallet_address, cutoff_ts, formation_n, formation_win_rate, formation_wilson_low,
             formation_median_return_pct, formation_avg_return_pct,
             validation_n, validation_win_rate, validation_wilson_low,
             validation_median_return_pct, validation_avg_return_pct, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (wallet_address) DO UPDATE SET
            cutoff_ts = excluded.cutoff_ts,
            formation_n = excluded.formation_n, formation_win_rate = excluded.formation_win_rate,
            formation_wilson_low = excluded.formation_wilson_low,
            formation_median_return_pct = excluded.formation_median_return_pct,
            formation_avg_return_pct = excluded.formation_avg_return_pct,
            validation_n = excluded.validation_n, validation_win_rate = excluded.validation_win_rate,
            validation_wilson_low = excluded.validation_wilson_low,
            validation_median_return_pct = excluded.validation_median_return_pct,
            validation_avg_return_pct = excluded.validation_avg_return_pct,
            computed_at = excluded.computed_at
        """,
        (
            wallet_address, cutoff_ts, f["n"], f["win_rate"], f["wilson_low"], f["median_return_pct"], f["avg_return_pct"],
            v["n"], v["win_rate"], v["wilson_low"], v["median_return_pct"], v["avg_return_pct"], now_ms2,
        ),
    )
    conn.commit()
    return {"formation": f, "validation": v, "cutoff_ts": cutoff_ts}


def run(wallet_address):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 8000")
    result = run_for_wallet(conn, wallet_address)
    if result is None:
        print(f"not enough data yet for a per-wallet walk-forward split (<{MIN_PER_HALF * 2} resolved copy positions)")
    else:
        f, v = result["formation"], result["validation"]
        print(f"formation:   n={f['n']:>4}  win_rate={f['win_rate']*100:5.1f}%  wilson_low={f['wilson_low']*100:5.1f}%  median_ret={f['median_return_pct']*100:6.1f}%")
        print(f"validation:  n={v['n']:>4}  win_rate={v['win_rate']*100:5.1f}%  wilson_low={v['wilson_low']*100:5.1f}%  median_ret={v['median_return_pct']*100:6.1f}%")
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/walkforward_single_wallet.py <wallet_address>")
        sys.exit(1)
    run(sys.argv[1])
