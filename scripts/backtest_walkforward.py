"""Walk-forward validation of the copy-trade backtest: does a wallet's
copy-trade performance in an earlier "formation" period predict its
performance in a later "validation" period, or is the ranking in
copy_trade_backtest just an elaborate description of history with no
forward-looking value?

Split: a single global cutoff at the midpoint of the dataset's timestamp
range (not a per-wallet midpoint -- the real question is "if I'd built
this and started trusting it partway through, would it have worked
afterward," which only a fixed calendar cutoff simulates honestly). Each
already-simulated copy position (see backtest_copy_trades.simulate_copy_trades)
is bucketed by resolved_ts, not entry_ts, since that's when its outcome
became knowable, same convention as api/main.py's value-timeline.

Only wallets with >= MIN_PER_HALF resolved copy positions on BOTH sides of
the cutoff are included -- below that, a period's win rate is close to a
coin flip's worth of noise regardless of Wilson adjustment.

Checks:
  1. Rank correlation (Spearman) between formation and validation metrics
     across all eligible wallets -- near zero means no persistence at all.
  2. Top-decile persistence -- do the wallets formation-period Wilson win
     rate would have flagged as best actually outperform the eligible
     population average in validation, or regress to the mean?
  3. Sign persistence -- of wallets with positive formation median return,
     what fraction still show positive validation median return?

No scipy in this venv, so Spearman correlation uses pandas' native rank
correlation and significance is assessed via a manual permutation test
(shuffle the pairing, recompute correlation, see how often chance alone
produces something this extreme) rather than a parametric p-value.
"""
import math
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH
from scripts.backtest_copy_trades import load_events, simulate_copy_trades, wilson_lower_bound

MIN_PER_HALF = 10
N_PERMUTATIONS = 5000
RNG_SEED = 0


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


def spearman_corr(a, b):
    """Spearman rank correlation without scipy (pandas' method="spearman"
    calls into scipy internally, which isn't installed in this venv) --
    rank both series and take the plain Pearson correlation of the ranks,
    which is the definition of Spearman's rho."""
    return pd.Series(a).rank().corr(pd.Series(b).rank())


def permutation_test(a, b, n_perm=N_PERMUTATIONS, seed=RNG_SEED):
    """Empirical two-sided p-value for Spearman correlation(a, b): how
    often does randomly shuffling b relative to a produce a correlation at
    least as extreme as the one actually observed?"""
    observed = spearman_corr(a, b)
    rng = np.random.default_rng(seed)
    b_arr = np.asarray(b)
    count = 0
    for _ in range(n_perm):
        shuffled = rng.permutation(b_arr)
        r = spearman_corr(a, shuffled)
        if abs(r) >= abs(observed):
            count += 1
    return observed, (count + 1) / (n_perm + 1)  # +1 avoids reporting p=0


def binom_two_sided_p(k, n, p=0.5):
    """Exact two-sided binomial test p-value, no scipy: sum the pmf of all
    outcomes at least as extreme (as unlikely or more) as observing k
    successes in n trials under the null p=0.5."""
    def pmf(i):
        return math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
    p_k = pmf(k)
    return min(1.0, sum(pmf(i) for i in range(n + 1) if pmf(i) <= p_k + 1e-12))


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    t0 = time.time()
    df = load_events(conn)
    print(f"loaded {len(df)} rows in {time.time()-t0:.1f}s")

    now_ms = int(time.time() * 1000)
    min_ts, max_ts = int(df["timestamp"].min()), int(df["timestamp"].max())
    cutoff_ts = (min_ts + max_ts) // 2
    print(f"formation: everything before {pd.Timestamp(cutoff_ts, unit='ms', tz='UTC')}")
    print(f"validation: everything from {pd.Timestamp(cutoff_ts, unit='ms', tz='UTC')} onward")

    t0 = time.time()
    positions, _, _ = simulate_copy_trades(df, now_ms)
    print(f"simulated {len(positions)} copy trades in {time.time()-t0:.1f}s")

    formation, validation = {}, {}
    for p in positions:
        bucket = formation if p["resolved_ts"] < cutoff_ts else validation
        bucket.setdefault(p["wallet_address"], []).append(p["return_pct"])

    eligible = sorted(set(formation) & set(validation))
    eligible = [w for w in eligible if len(formation[w]) >= MIN_PER_HALF and len(validation[w]) >= MIN_PER_HALF]
    print(f"{len(eligible)} wallets have {MIN_PER_HALF}+ resolved copy positions on both sides of the cutoff")

    if len(eligible) < 10:
        print("too few eligible wallets to say anything statistically meaningful -- stopping")
        conn.close()
        return

    rows = []
    for w in eligible:
        f = period_stats(formation[w])
        v = period_stats(validation[w])
        rows.append({"wallet_address": w, **{f"formation_{k}": val for k, val in f.items()},
                     **{f"validation_{k}": val for k, val in v.items()}})
    result_df = pd.DataFrame(rows)

    # 1. rank correlation + permutation significance, for each metric
    print("\n--- rank correlation (formation vs validation), permutation p-value ---")
    metric_results = {}
    for metric in ["wilson_low", "win_rate", "median_return_pct", "avg_return_pct"]:
        r, p = permutation_test(result_df[f"formation_{metric}"], result_df[f"validation_{metric}"])
        metric_results[metric] = (r, p)
        print(f"{metric:<20} spearman r = {r:+.3f}   p = {p:.4f}")

    # 2. top-decile persistence, by formation wilson_low
    top_n = max(1, round(len(result_df) * 0.2))
    top = result_df.nlargest(top_n, "formation_wilson_low")
    rest_mean = result_df["validation_win_rate"].mean()
    top_mean = top["validation_win_rate"].mean()
    print(f"\n--- top-quintile persistence (n={top_n} of {len(result_df)}, by formation wilson_low) ---")
    print(f"population validation win rate (all {len(result_df)} eligible wallets): {rest_mean*100:.1f}%")
    print(f"top-quintile-by-formation validation win rate: {top_mean*100:.1f}%")

    # 3. sign persistence on median return
    pos_formation = result_df[result_df["formation_median_return_pct"] > 0]
    sign_persist_n = len(pos_formation)
    sign_persist_k = (pos_formation["validation_median_return_pct"] > 0).sum()
    sign_persist_rate = sign_persist_k / sign_persist_n if sign_persist_n else None
    sign_p = binom_two_sided_p(int(sign_persist_k), sign_persist_n) if sign_persist_n else None
    print(f"\n--- sign persistence (formation median return > 0) ---")
    print(f"{sign_persist_k}/{sign_persist_n} ({sign_persist_rate*100:.1f}%) also validation median return > 0"
          f" (binomial p={sign_p:.4f} vs 50% baseline)" if sign_persist_n else "no wallets with positive formation median return")

    # persist per-wallet rows
    conn.execute("DELETE FROM walkforward_backtest")
    now_ms2 = int(time.time() * 1000)
    conn.executemany(
        """
        INSERT INTO walkforward_backtest
            (wallet_address, cutoff_ts, formation_n, formation_win_rate, formation_wilson_low,
             formation_median_return_pct, formation_avg_return_pct,
             validation_n, validation_win_rate, validation_wilson_low,
             validation_median_return_pct, validation_avg_return_pct, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r["wallet_address"], cutoff_ts, r["formation_n"], r["formation_win_rate"], r["formation_wilson_low"],
                r["formation_median_return_pct"], r["formation_avg_return_pct"],
                r["validation_n"], r["validation_win_rate"], r["validation_wilson_low"],
                r["validation_median_return_pct"], r["validation_avg_return_pct"], now_ms2,
            )
            for r in rows
        ],
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    print(f"\nwrote {len(rows)} walkforward_backtest rows")
    conn.close()

    result_df.to_csv(Path(__file__).resolve().parent.parent / "data" / "walkforward_results.csv", index=False)
    print("also wrote data/walkforward_results.csv for the report")


if __name__ == "__main__":
    run()
