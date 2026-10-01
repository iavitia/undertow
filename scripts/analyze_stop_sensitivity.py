"""Stop-loss sensitivity sweep + historical MAE/MFE (Maximum Adverse /
Favorable Excursion), computed together since both need the same thing:
the full intrinsic-value path a position's underlying strike traced
between entry and resolution.

The single 3x stop scripts/backtest_stop_loss.py tests was a plausible
starting point, not evidence that 3x in particular is a good threshold. A
first coarse sweep [1.5, 2.0, 2.5, 3.0, 4.0, 5.0] found 1.5x -- the
tightest level tested -- winning on every metric on both wallet
populations, monotonically, with 1.5x sitting right at the edge of the
search space. This zooms a fine grid into [1.1..2.5] (0.1 steps) to see
whether that's a genuine plateau/robust region or a search-boundary
artifact, and adds three things worth having: dollar-weighted P&L
saved-vs-forfeited (not just percentage), stop-slippage/gap statistics
(how far a triggered stop's real exit overshoots its clean theoretical
cap), and per-WALLET (not just per-population) MAE distributions, which
feed scripts/analyze_stop_wallet_correlation.py's check of whether
wallets hurt by a tight stop share a measurable characteristic.

Historical-data limitation carried over from backtest_stop_loss.py,
unchanged: there's no historical option mark-price/greeks data for
arbitrary past timestamps, so intrinsic value (nearest spot print +
strike, ignoring time value) is the best available proxy for "cost to
buy back at that moment." This understates how early a real stop with
live mark pricing would trigger, if anything -- see that module's
docstring for the full reasoning."""
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH, HYPOTHETICAL_STAKE_USD
from scripts.backtest_copy_trades import build_spot_price_index, load_events, simulate_copy_trades
from scripts.backtest_stop_loss import MIN_ENTRY_PRICE, TARGET_WALLET_ALIASES

COARSE_MULTIPLES = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
FINE_MULTIPLES = [round(1.1 + 0.1 * i, 1) for i in range(15)]  # 1.1 .. 2.5
# Below 1.1x: the coarse+fine sweep found total return still improving
# monotonically all the way to the 1.1x edge with no interior peak found
# (see conversation) -- this extends toward 1.0x to actually find where
# the curve turns over, if it does before the point where "stop-loss" and
# "never take the naked side" become indistinguishable.
ULTRA_FINE_MULTIPLES = [1.01, 1.025, 1.05, 1.075, 1.10, 1.15, 1.20, 1.30, 1.50]
STOP_MULTIPLES = sorted(set(COARSE_MULTIPLES) | set(FINE_MULTIPLES) | set(ULTRA_FINE_MULTIPLES))


def position_excursion_path(spot_index, asset, strike, option_type, entry_ts, resolved_ts):
    """The full intrinsic-value array between entry_ts and resolved_ts
    (inclusive), sliced via np.searchsorted the same way
    backtest_stop_loss.find_stop_out does for a single threshold --
    generalized here to return the whole path so MAE/MFE and multiple
    stop-threshold crossings can all be read off it without re-slicing.
    Returns (window_ts, values) numpy arrays, possibly empty."""
    timestamps, prices = spot_index.get(asset, (np.array([]), np.array([])))
    if len(timestamps) == 0:
        return np.array([]), np.array([])
    lo = np.searchsorted(timestamps, entry_ts, side="left")
    hi = np.searchsorted(timestamps, resolved_ts, side="right")
    if lo >= hi:
        return np.array([]), np.array([])
    window_ts, window_prices = timestamps[lo:hi], prices[lo:hi]
    if option_type == "call":
        values = np.maximum(window_prices - strike, 0)
    else:
        values = np.maximum(strike - window_prices, 0)
    return window_ts, values


def first_crossing(window_ts, values, threshold):
    hits = np.nonzero(values >= threshold)[0]
    if len(hits) == 0:
        return None, None
    i = hits[0]
    return int(window_ts[i]), float(values[i])


def max_drawdown(returns_with_ts):
    """returns_with_ts: list of (resolved_ts, return_pct). Sorts
    chronologically, builds a $10-normalized cumulative running total, and
    returns the largest peak-to-trough drop on that curve. Explicitly a
    simplification -- independent positions summed in resolution order,
    not a real funded-portfolio equity curve with overlap/margin -- same
    treatment total_return_pct already gets everywhere else in this
    project (a flat sum, not compounded)."""
    if not returns_with_ts:
        return 0.0
    ordered = sorted(returns_with_ts, key=lambda x: x[0])
    cum = 0.0
    peak = 0.0
    worst_dd = 0.0
    for _, r in ordered:
        cum += r * HYPOTHETICAL_STAKE_USD
        peak = max(peak, cum)
        worst_dd = min(worst_dd, cum - peak)
    return worst_dd


def run(stop_multiples=STOP_MULTIPLES):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    blown_up_addrs = set()
    for alias in TARGET_WALLET_ALIASES:
        row = conn.execute("SELECT address FROM wallets WHERE alias = ?", (alias,)).fetchone()
        if row:
            blown_up_addrs.add(row["address"])
    watchlist_addrs = {
        r["wallet_address"] for r in conn.execute("SELECT wallet_address FROM watchlist WHERE is_active = 1").fetchall()
    }
    wallet_sets = {"blown_up_8": blown_up_addrs, "current_watchlist": watchlist_addrs}
    all_target_addrs = blown_up_addrs | watchlist_addrs

    t0 = time.time()
    df = load_events(conn)
    print(f"loaded {len(df)} rows in {time.time()-t0:.1f}s")

    now_ms = int(time.time() * 1000)
    t0 = time.time()
    positions, _, _ = simulate_copy_trades(df, now_ms)
    print(f"simulated {len(positions)} copy trades in {time.time()-t0:.1f}s")

    spot_index = build_spot_price_index(df)

    target_positions = [
        p for p in positions
        if p["wallet_address"] in all_target_addrs and p["entry_price"] >= MIN_ENTRY_PRICE[p["asset"]]
    ]
    print(f"{len(target_positions)} positions across {len(all_target_addrs)} target wallet(s), sweeping {len(stop_multiples)} stop levels")

    now_ms2 = int(time.time() * 1000)
    for tbl in ("stop_sensitivity", "position_mae_mfe", "wallet_mae_summary"):
        conn.execute(f"DELETE FROM {tbl} WHERE wallet_set IN ('blown_up_8', 'current_watchlist')")

    # Per position: excursion path, MAE/MFE, and first-crossing (both
    # return and raw stop_value, for overshoot) for every stop multiple,
    # all from one slice.
    enriched = []
    mae_mfe_rows = []
    for p in target_positions:
        entry = {"protected_at": {}, "stop_value_at": {}, "time_to_stop_at": {}, "first_print_hit_at": {}}
        if p["entry_side"] == "sell":
            window_ts, values = position_excursion_path(
                spot_index, p["asset"], p["strike"], p["option_type"], p["entry_ts"], p["resolved_ts"]
            )
            if len(values) > 0:
                mae_multiple = float(values.max()) / p["entry_price"]
                mfe_multiple = float(values.min()) / p["entry_price"]
            else:
                mae_multiple = mfe_multiple = None
            first_print_ts = int(window_ts[0]) if len(window_ts) > 0 else None
            for mult in stop_multiples:
                if len(values) == 0:
                    entry["protected_at"][mult] = p["return_pct"]
                    continue
                stop_ts, stop_value = first_crossing(window_ts, values, mult * p["entry_price"])
                if stop_value is None:
                    entry["protected_at"][mult] = p["return_pct"]
                else:
                    entry["protected_at"][mult] = (p["entry_price"] - stop_value) / p["entry_price"]
                    entry["stop_value_at"][mult] = stop_value
                    entry["time_to_stop_at"][mult] = (stop_ts - p["entry_ts"]) / 3_600_000  # hours
                    entry["first_print_hit_at"][mult] = (stop_ts == first_print_ts)

            for wallet_set, addrs in wallet_sets.items():
                if p["wallet_address"] in addrs:
                    mae_mfe_rows.append((
                        p["wallet_address"], wallet_set, p["instrument"], p["entry_price"],
                        mae_multiple, mfe_multiple, p["return_pct"], now_ms2,
                    ))
        entry.update(p)
        enriched.append(entry)

    conn.executemany(
        """
        INSERT INTO position_mae_mfe
            (wallet_address, wallet_set, instrument, entry_price, mae_multiple, mfe_multiple, actual_return_pct, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        mae_mfe_rows,
    )

    # Per-wallet MAE summary, winner/loser split -- input for
    # analyze_stop_wallet_correlation.py.
    wallet_mae_rows = []
    for wallet_set, addrs in wallet_sets.items():
        by_wallet = {}
        for r in mae_mfe_rows:
            if r[1] != wallet_set:
                continue
            by_wallet.setdefault(r[0], []).append((r[4], r[6]))  # (mae_multiple, actual_return_pct)
        for wallet, rows in by_wallet.items():
            for group, cond in (("winner", lambda x: x[1] > 0), ("loser", lambda x: x[1] <= 0)):
                maes = [x[0] for x in rows if cond(x) and x[0] is not None]
                if not maes:
                    continue
                wallet_mae_rows.append((
                    wallet, wallet_set, group, len(maes),
                    statistics.median(maes),
                    float(np.percentile(maes, 90)),
                    now_ms2,
                ))
    conn.executemany(
        """
        INSERT INTO wallet_mae_summary
            (wallet_address, wallet_set, outcome_group, n, median_mae_multiple, p90_mae_multiple, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        wallet_mae_rows,
    )

    def profit_factor(returns):
        gains = sum(r for r in returns if r > 0)
        losses = abs(sum(r for r in returns if r < 0))
        return gains / losses if losses > 0 else None

    def pctl(values, p):
        return float(np.percentile(values, p)) if values else None

    sweep_levels = [None] + stop_multiples  # None = no-stop baseline
    rows_out = []
    report_lines = []

    for wallet_set, addrs in wallet_sets.items():
        set_positions = [p for p in enriched if p["wallet_address"] in addrs]
        for mult in sweep_levels:
            if mult is None:
                returns = [p["return_pct"] for p in set_positions]
                returns_with_ts = [(p["resolved_ts"], p["return_pct"]) for p in set_positions]
                returns_excl_fp = returns
                n_stopped = n_whipsaw = 0
                pnl_saved = pnl_forfeited = 0.0
                overshoots = []
                times_to_stop = []
                n_first_print = 0
            else:
                returns = [p["protected_at"].get(mult, p["return_pct"]) if p["entry_side"] == "sell" else p["return_pct"] for p in set_positions]
                returns_with_ts = [
                    (p["resolved_ts"], p["protected_at"].get(mult, p["return_pct"]) if p["entry_side"] == "sell" else p["return_pct"])
                    for p in set_positions
                ]
                # Same as `returns`, but any position whose stop fired on the
                # very first available print falls back to its real,
                # unprotected outcome -- isolates how much of this level's
                # advantage is genuine mid-life risk management vs. the
                # entry-confirmation artifact.
                returns_excl_fp = [
                    p["return_pct"] if (p["entry_side"] == "sell" and p["first_print_hit_at"].get(mult))
                    else (p["protected_at"].get(mult, p["return_pct"]) if p["entry_side"] == "sell" else p["return_pct"])
                    for p in set_positions
                ]
                n_stopped = n_whipsaw = 0
                pnl_saved = pnl_forfeited = 0.0
                overshoots = []
                times_to_stop = []
                n_first_print = 0
                for p in set_positions:
                    if p["entry_side"] != "sell":
                        continue
                    protected = p["protected_at"].get(mult)
                    if protected is None or protected == p["return_pct"]:
                        continue  # never triggered at this multiple
                    n_stopped += 1
                    if protected > p["return_pct"]:
                        pnl_saved += protected - p["return_pct"]
                    elif protected < p["return_pct"]:
                        n_whipsaw += 1
                        pnl_forfeited += p["return_pct"] - protected
                    stop_value = p["stop_value_at"].get(mult)
                    if stop_value is not None:
                        theoretical = mult * p["entry_price"]
                        overshoots.append(stop_value / theoretical - 1)
                    times_to_stop.append(p["time_to_stop_at"][mult])
                    if p["first_print_hit_at"].get(mult):
                        n_first_print += 1

            n = len(returns)
            if n == 0:
                continue
            wins = sum(1 for r in returns if r > 0)
            avg_overshoot = statistics.mean(overshoots) if overshoots else None
            median_overshoot = statistics.median(overshoots) if overshoots else None
            p90_overshoot = pctl(overshoots, 90)
            median_tts = statistics.median(times_to_stop) if times_to_stop else None
            p10_tts = pctl(times_to_stop, 10)
            p90_tts = pctl(times_to_stop, 90)
            pct_first_print = (n_first_print / n_stopped) if n_stopped else None
            # Net benefit vs. the no-stop baseline, using the
            # excl-first-print returns instead of the raw protected ones.
            baseline_total = sum(p["return_pct"] for p in set_positions)
            net_usd_excl_fp = (sum(returns_excl_fp) - baseline_total) * HYPOTHETICAL_STAKE_USD
            rows_out.append((
                wallet_set, mult, n, wins / n, sum(returns), min(returns),
                profit_factor(returns), max_drawdown(returns_with_ts),
                n_stopped, pnl_saved, pnl_forfeited, n_whipsaw,
                (n_whipsaw / n_stopped) if n_stopped else None,
                pnl_saved * HYPOTHETICAL_STAKE_USD, pnl_forfeited * HYPOTHETICAL_STAKE_USD,
                (pnl_saved - pnl_forfeited) * HYPOTHETICAL_STAKE_USD,
                avg_overshoot, median_overshoot, p90_overshoot,
                median_tts, p10_tts, p90_tts, pct_first_print,
                sum(returns_excl_fp) * 100, net_usd_excl_fp,
                now_ms2,
            ))
            label = "no-stop" if mult is None else f"{mult}x"
            pf = profit_factor(returns)
            net_usd = (pnl_saved - pnl_forfeited) * HYPOTHETICAL_STAKE_USD
            report_lines.append(
                f"{wallet_set:<18}{label:<9}{n:>6}{wins/n*100:>7.1f}%{sum(returns)*100:>12.1f}%"
                f"{(pf if pf else 0):>7.2f}{net_usd:>11,.0f}"
                f"{n_stopped:>8}"
                f"{(median_tts if median_tts is not None else 0):>10.1f}"
                f"{(pct_first_print*100 if pct_first_print is not None else 0):>9.1f}%"
                f"{net_usd_excl_fp:>13,.0f}"
            )

    conn.executemany(
        """
        INSERT INTO stop_sensitivity
            (wallet_set, stop_multiple, n, win_rate, total_return_pct, worst_return_pct, profit_factor,
             max_drawdown_usd, n_stopped, pnl_saved_pct, pnl_forfeited_pct, n_whipsaw, false_stop_rate,
             pnl_saved_usd, pnl_forfeited_usd, net_benefit_usd,
             avg_overshoot_pct, median_overshoot_pct, p90_overshoot_pct,
             median_time_to_stop_hours, p10_time_to_stop_hours, p90_time_to_stop_hours, pct_triggered_at_first_print,
             total_return_pct_excl_first_print, net_benefit_usd_excl_first_print,
             computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_out,
    )
    conn.commit()

    print(f"\n{'wallet_set':<18}{'stop':<9}{'n':>6}{'win%':>8}{'total_ret%':>13}{'PF':>7}{'net$':>11}{'stopped':>8}{'medTTS(h)':>11}{'1stPrint%':>10}{'net$exclFP':>13}")
    for line in report_lines:
        print(line)

    conn.close()


if __name__ == "__main__":
    run()
