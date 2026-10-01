"""Calibration study: does an entry that's unusually large relative to a
wallet's OWN trading history (analysis/sizing.py's size_ratio) actually
predict worse outcomes, across the full wallet universe -- not just the 8
wallets this pattern was first spotted in by hand (see conversation)?

Buckets every resolved copy-simulated position by size_ratio, separately
for buy and sell (a buy's max loss is already capped at 100% of premium
regardless of size, in return-on-premium terms -- a sell's isn't, so
there's a real reason these might behave differently). Reports n /
win_rate / avg_return_pct / total_return_pct per bucket -- this is where
scripts/backtest_stop_loss.py's size_gated mode's threshold comes from,
picked from wherever the data shows a real divergence, not guessed."""
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH
from scripts.backtest_copy_trades import load_events, simulate_copy_trades
from analysis.sizing import compute_entry_size_ratios

# Real minimum tick sizes confirmed live via Derive's /public/get_ticker
# (see scripts/backtest_stop_loss.py, where this same floor was needed
# first): below this, return-on-premium is numerically degenerate -- one
# BTC position entered at $0.0001 produced a -74,999,900% single-position
# return that swamped an entire bucket's average by itself. Applied here
# too, for the same reason.
MIN_ENTRY_PRICE = {"BTC": 1.0, "ETH": 0.10}

BUCKETS = [
    (0, 1.0, "<1x"),
    (1.0, 1.5, "1-1.5x"),
    (1.5, 2.0, "1.5-2x"),
    (2.0, 3.0, "2-3x"),
    (3.0, 5.0, "3-5x"),
    (5.0, float("inf"), "5x+"),
]


def bucket_for(ratio):
    for lo, hi, label in BUCKETS:
        if lo <= ratio < hi:
            return label
    return None


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

    ratios = compute_entry_size_ratios(df)
    ratio_lookup = ratios["size_ratio"].to_dict()  # (wallet_address, instrument) -> size_ratio or NaN

    groups = {}  # (side, bucket_label) -> list of return_pct
    n_no_ratio = 0
    n_sub_tick = 0
    for p in positions:
        if p["entry_price"] < MIN_ENTRY_PRICE[p["asset"]]:
            n_sub_tick += 1
            continue
        ratio = ratio_lookup.get((p["wallet_address"], p["instrument"]))
        if ratio is None or ratio != ratio:  # NaN check without importing math/numpy here
            n_no_ratio += 1
            continue
        label = bucket_for(ratio)
        groups.setdefault((p["entry_side"], label), []).append(p["return_pct"])

    print(f"\n{n_sub_tick} positions excluded (entry price below the real minimum tick size)")
    print(f"{n_no_ratio} positions excluded (not enough of that wallet's own prior history yet to judge)\n")
    print(f"{'side':<6}{'bucket':<10}{'n':>8}{'win%':>8}{'avg_ret%':>12}{'median_ret%':>14}{'total_ret%':>14}")
    for side in ("sell", "buy"):
        for _, _, label in BUCKETS:
            returns = groups.get((side, label), [])
            if not returns:
                print(f"{side:<6}{label:<10}{0:>8}{'n/a':>8}{'n/a':>12}{'n/a':>14}{'n/a':>14}")
                continue
            n = len(returns)
            wins = sum(1 for r in returns if r > 0)
            avg_ret = sum(returns) / n
            median_ret = statistics.median(returns)
            total_ret = sum(returns)
            print(f"{side:<6}{label:<10}{n:>8}{wins/n*100:>7.1f}%{avg_ret*100:>11.1f}%{median_ret*100:>13.1f}%{total_ret*100:>13.1f}%")

    conn.close()


if __name__ == "__main__":
    run()
