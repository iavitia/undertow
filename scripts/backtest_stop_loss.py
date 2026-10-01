"""Would a stop-loss have actually saved the wallets that blew up?

Investigated in detail earlier (see conversation): cedar-ridge-e9,
iron-grove-22, cedar-basin-72, north-quarry-44, copper-current-1e,
drift-grove-08, glacial-thicket-ce, faded-summit-12 all had good win
rates wiped out by a small number of naked shorts held to expiry with
no stop-loss, several sized up well above the wallet's own average.
This replays their scripts/backtest_copy_trades.py positions three ways --
'actual' (unmodified, what really would have happened copying them, naked
shorts included), 'protected' (naked shorts capped at STOP_MULTIPLE x the
premium collected), and 'skipped' (naked shorts excluded entirely --
buy-side positions only, matching what run_watchlist_agent.py's live gate
already does today) -- and reports the delta between all three.

Historical limitation, worth stating plainly: there's no historical
option mark-price/greeks data available for arbitrary past timestamps --
Derive's ticker only exposes the *current* live price. The best available
proxy here is the same expiry-settlement approach already validated
everywhere in this project (analysis/pnl.py's nearest_index_price +
intrinsic_value), applied at intermediate points in a position's life
instead of only at expiry. Intrinsic value ignores time value, so this
systematically UNDERSTATES how early a real stop would trigger -- a short
call's real market price already reflects extrinsic value before it's
even in the money. This is a conservative, best-available backtest proxy,
not a claim that live mark-price polling (not built yet) would behave
identically -- if anything, a real implementation would likely cut losses
earlier and look better than this.

Only naked shorts (entry_side == 'sell') are ever adjusted -- a bought
option is already capped at 100% of premium paid.

STOP_MULTIPLE default (see conversation, scripts/analyze_stop_sensitivity.py
and scripts/analyze_stop_wallet_correlation.py): settled at 1.10x after a
full sensitivity sweep from 1.01x to 5.0x on both the blown-up-8 and
current-watchlist populations. The swept curve never found a true interior
peak -- total return kept improving monotonically all the way down to
1.01x on both populations -- so 1.10x isn't "the optimum," it's a point on
a flattening curve chosen deliberately short of the tightest tested levels
(1.01x-1.05x), which showed a real, measurable (if minority, ~9.4% of
triggers on blown_up_8) entry-confirmation artifact -- stops firing on the
very first available price print after entry, i.e. detecting an already-
stale copy-entry rather than genuine mid-life deterioration. That artifact
was then directly tested by excluding every first-print-triggered stop and
recomputing: it barely moved the numbers (well under 1%, and net-positive
on the watchlist), confirming the tight-stop advantage is real and not an
artifact -- but 1.10x (0.6-0.7% first-print rate) was chosen over the
untested/uncomfortable edge at 1.01x-1.05x as the more defensible point on
an already-flat part of the curve. Confirmed to outperform the 'skipped'
mode (naked shorts excluded entirely, what run_watchlist_agent.py's live
gate currently does) by a wide margin on both populations."""
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH
from config import STOP_MULTIPLE  # shared with scripts/run_watchlist_agent.py's live stop-loss engine -- see config.py's comment for how 1.10x was chosen
from scripts.backtest_copy_trades import build_spot_price_index, load_events, simulate_copy_trades

# Real minimum tick sizes confirmed live via Derive's /public/get_ticker
# earlier this session (BTC: $1, ETH: $0.10). Below that, "return on
# premium" is numerically degenerate -- a real position at that price
# couldn't even be opened at Derive's own minimum granularity, and a tiny
# absolute price move produces an enormous, economically meaningless
# percentage swing (found empirically: one BTC call sold for $0.0475 right
# before expiring worthless produced a -26,760% "protected" return off a
# real, small absolute move, swamping an otherwise-solid wallet's total).
# Applied consistently across all three modes so no comparison is skewed
# by excluding it from only one.
MIN_ENTRY_PRICE = {"BTC": 1.0, "ETH": 0.10}

TARGET_WALLET_ALIASES = [
    "cedar-ridge-e9", "iron-grove-22", "cedar-basin-72", "north-quarry-44",
    "copper-current-1e", "drift-grove-08", "glacial-thicket-ce", "faded-summit-12",
]


def find_stop_out(spot_index, asset, strike, option_type, entry_price, entry_ts, resolved_ts, stop_multiple):
    """First available spot print between entry_ts and resolved_ts
    (inclusive) where intrinsic value reaches stop_multiple x entry_price
    -- the point at which buying back this short would cost that many
    multiples of what was collected. Returns (stop_ts, stop_value) or
    (None, None) if never breached in that window. Uses np.searchsorted
    to slice straight to the relevant window rather than scanning the
    asset's full price history per position (matches the vectorized style
    already used by backtest_copy_trades.nearest_spot_price)."""
    timestamps, prices = spot_index.get(asset, (np.array([]), np.array([])))
    if len(timestamps) == 0:
        return None, None
    lo = np.searchsorted(timestamps, entry_ts, side="left")
    hi = np.searchsorted(timestamps, resolved_ts, side="right")
    if lo >= hi:
        return None, None
    window_ts, window_prices = timestamps[lo:hi], prices[lo:hi]
    if option_type == "call":
        values = np.maximum(window_prices - strike, 0)
    else:
        values = np.maximum(strike - window_prices, 0)
    threshold = stop_multiple * entry_price
    hits = np.nonzero(values >= threshold)[0]
    if len(hits) == 0:
        return None, None
    i = hits[0]
    return int(window_ts[i]), float(values[i])


def run(target_aliases=TARGET_WALLET_ALIASES, stop_multiple=STOP_MULTIPLE):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    addr_to_alias = {}
    for alias in target_aliases:
        row = conn.execute("SELECT address FROM wallets WHERE alias = ?", (alias,)).fetchone()
        if row is None:
            print(f"WARNING: no wallet found for alias {alias!r}, skipping")
            continue
        addr_to_alias[row["address"]] = alias
    target_addresses = set(addr_to_alias)

    t0 = time.time()
    df = load_events(conn)
    print(f"loaded {len(df)} rows in {time.time()-t0:.1f}s")

    now_ms = int(time.time() * 1000)
    t0 = time.time()
    positions, _, _ = simulate_copy_trades(df, now_ms)
    print(f"simulated {len(positions)} copy trades in {time.time()-t0:.1f}s")

    spot_index = build_spot_price_index(df)

    target_positions = [p for p in positions if p["wallet_address"] in target_addresses]
    n_before_floor = len(target_positions)
    target_positions = [p for p in target_positions if p["entry_price"] >= MIN_ENTRY_PRICE[p["asset"]]]
    n_excluded = n_before_floor - len(target_positions)
    print(f"{len(target_positions)} positions across {len(target_addresses)} target wallet(s)"
          + (f" ({n_excluded} excluded below the real minimum tick size)" if n_excluded else ""))

    now_ms2 = int(time.time() * 1000)
    conn.execute("DELETE FROM stop_loss_backtest WHERE wallet_address IN ({})".format(
        ",".join("?" * len(target_addresses))), tuple(target_addresses))
    conn.execute("DELETE FROM stop_loss_backtest_stops WHERE wallet_address IN ({})".format(
        ",".join("?" * len(target_addresses))), tuple(target_addresses))

    by_wallet_actual = {}
    by_wallet_protected = {}
    by_wallet_skipped = {}  # buy-side positions only -- naked shorts excluded entirely, not just capped
    stop_rows = []

    for p in target_positions:
        wallet = p["wallet_address"]
        by_wallet_actual.setdefault(wallet, []).append(p["return_pct"])
        if p["entry_side"] == "buy":
            by_wallet_skipped.setdefault(wallet, []).append(p["return_pct"])

        protected_return = p["return_pct"]
        if p["entry_side"] == "sell":
            stop_ts, stop_value = find_stop_out(
                spot_index, p["asset"], p["strike"], p["option_type"],
                p["entry_price"], p["entry_ts"], p["resolved_ts"], stop_multiple,
            )
            if stop_ts is not None:
                protected_return = (p["entry_price"] - stop_value) / p["entry_price"]
                # sell: return_pct = (entry_price - exit_value) / entry_price -- invert to
                # recover the original exit_value for the detail row, rather than a second pass.
                original_exit_value = p["entry_price"] * (1 - p["return_pct"])
                stop_rows.append((
                    wallet, p["instrument"], p["entry_price"], original_exit_value, p["return_pct"], p["resolved_ts"],
                    stop_value, protected_return, stop_ts, stop_multiple, now_ms2,
                ))
        by_wallet_protected.setdefault(wallet, []).append(protected_return)

    conn.executemany(
        """
        INSERT INTO stop_loss_backtest_stops
            (wallet_address, instrument, entry_price, original_exit_value, original_return_pct,
             original_resolved_ts, stopped_exit_value, stopped_return_pct, stop_ts, stop_multiple, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        stop_rows,
    )

    def summarize(by_wallet, n_stopped_by_wallet, mode):
        rows_out = []
        for wallet, returns in by_wallet.items():
            n = len(returns)
            wins = sum(1 for r in returns if r > 0)
            rows_out.append((
                wallet, mode, n, wins, wins / n if n else None, sum(returns), min(returns), max(returns),
                n_stopped_by_wallet.get(wallet, 0), stop_multiple, now_ms2,
            ))
        return rows_out

    n_stopped_by_wallet = {}
    for row in stop_rows:
        n_stopped_by_wallet[row[0]] = n_stopped_by_wallet.get(row[0], 0) + 1

    rows_out = (
        summarize(by_wallet_actual, {}, "actual")
        + summarize(by_wallet_protected, n_stopped_by_wallet, "protected")
        + summarize(by_wallet_skipped, {}, "skipped")
    )
    conn.executemany(
        """
        INSERT INTO stop_loss_backtest
            (wallet_address, mode, n, wins, win_rate, total_return_pct, worst_return_pct, best_return_pct,
             n_stopped_out, stop_multiple, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_out,
    )
    conn.commit()

    print(f"\n{'wallet':<20}{'mode':<12}{'n':>5}{'win%':>8}{'total_ret%':>14}{'worst%':>12}{'stopped':>10}")
    for wallet in target_addresses:
        alias = addr_to_alias[wallet]
        for mode, by_wallet in (("actual", by_wallet_actual), ("protected", by_wallet_protected), ("skipped", by_wallet_skipped)):
            returns = by_wallet.get(wallet, [])
            if not returns:
                print(f"{alias:<20}{mode:<12}{'0':>5}{'n/a':>8}{'n/a':>14}{'n/a':>12}{0:>10}")
                continue
            n = len(returns)
            wins = sum(1 for r in returns if r > 0)
            n_stopped = n_stopped_by_wallet.get(wallet, 0) if mode == "protected" else 0
            print(f"{alias:<20}{mode:<12}{n:>5}{wins/n*100:>7.1f}%{sum(returns)*100:>13.1f}%{min(returns)*100:>11.1f}%{n_stopped:>10}")

    conn.close()


if __name__ == "__main__":
    run()
