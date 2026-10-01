"""Historical backtest: if a copier had mirrored every position a wallet
opened -- entering not at the wallet's own fill but at the next available
market print from any OTHER wallet in that instrument, since a real copier
sees the trade after the fact -- would it have been profitable?

This answers a different question than wallet_leaderboard (that wallet's
own win rate). A wallet can have a great track record on its own fills and
still be a bad copy target if its edge comes from price/timing that a
follower can't realistically get.

Outcome pricing:
  - Position closed by the wallet's own offsetting trade: copy exit price
    is the next other-wallet print in that instrument after the wallet's
    close, mirroring the copy-entry logic.
  - Position never closed but its instrument has since expired: copy exit
    uses the same expiry-settlement estimate as compute_wallet_positions
    in api/main.py (nearest index price, any wallet, to the expiry
    timestamp) -- this part isn't wallet-specific, so no copy-price lookup
    needed.
  - Position still open, not yet expired: excluded, no outcome yet.

A copy price further than STALENESS_WINDOW_MS after the reference
timestamp is treated as not realistically copyable (the instrument went
quiet) and excluded rather than used stale.

RFQ trades are excluded as a copy-price source if they share an rfq_id
the wallet itself used for that instrument: an RFQ deal is one bilateral
transaction split into two DB rows (maker + taker) at a single pre-agreed
price, so the "next print by another wallet" would otherwise very often
just be the guaranteed other half of the wallet's OWN trade -- confirmed
empirically (see conversation: one wallet's "next print" was its RFQ
counterparty 6.7 seconds later at the identical price, for 298 of its 304
trades). That's not an independent, later-observable market price a real
copier could have gotten; it's circular. ~19% of all trade legs are
RFQ-matched, so this isn't a rare edge case.

Returns are expressed as return-on-premium (the standard options
convention): for a buy, (exit_value - entry_price) / entry_price; for a
sell, (entry_price - exit_value) / entry_price. Fees are not modeled -- a
copier's fee schedule would differ from the original wallet's anyway.
"""
import math
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import COPY_STALENESS_WINDOW_MS as STALENESS_WINDOW_MS
from config import DB_PATH


def wilson_lower_bound(wins, n, z=1.96):
    """95% Wilson score lower bound on a win rate -- penalizes small
    samples instead of trusting a raw win_rate that a handful of trades
    could produce by chance. This, not raw win_rate, is what results
    should be ranked by."""
    if n == 0:
        return None
    phat = wins / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return (center - margin) / denom


def build_instrument_price_index(df):
    """instrument -> (timestamps, wallet_addresses, prices, rfq_ids), each
    sorted by timestamp, for the "next print by another wallet" lookup."""
    index = {}
    for instrument, grp in df.groupby("instrument", sort=False):
        g = grp.sort_values("timestamp")
        index[instrument] = (
            g["timestamp"].to_numpy(),
            g["wallet_address"].to_numpy(),
            g["price"].to_numpy(),
            g["rfq_id"].to_numpy(),
        )
    return index


def next_other_wallet_print(price_index, instrument, wallet_address, after_ts, excluded_rfq_ids):
    """First trade in this instrument, by a wallet other than
    wallet_address, at or after after_ts, within STALENESS_WINDOW_MS, and
    not sharing an rfq_id this wallet itself used for this instrument (see
    module docstring -- an RFQ deal's two legs land at an identical
    pre-agreed price seconds apart, so without this exclusion the "next
    print" is very often just the guaranteed other half of the wallet's
    own trade). Returns (price, timestamp) or (None, None)."""
    timestamps, wallets, prices, rfq_ids = price_index[instrument]
    start = np.searchsorted(timestamps, after_ts, side="left")
    cutoff = after_ts + STALENESS_WINDOW_MS
    for i in range(start, len(timestamps)):
        if timestamps[i] > cutoff:
            return None, None
        if wallets[i] == wallet_address:
            continue
        if rfq_ids[i] and rfq_ids[i] in excluded_rfq_ids:
            continue
        return prices[i], timestamps[i]
    return None, None


def build_spot_price_index(df):
    """asset -> (timestamps, index_price_usd) sorted, for expiry-settlement
    lookups -- same nearest-price approach as api/main.py's
    nearest_index_price, vectorized here since this runs for every
    expired-with-no-close position across every wallet."""
    index = {}
    for asset, grp in df.groupby("asset", sort=False):
        g = grp[["timestamp", "index_price_usd"]].dropna().sort_values("timestamp")
        index[asset] = (g["timestamp"].to_numpy(), g["index_price_usd"].to_numpy())
    return index


def nearest_spot_price(spot_index, asset, ts_ms):
    timestamps, prices = spot_index.get(asset, (np.array([]), np.array([])))
    if len(timestamps) == 0:
        return None
    i = np.searchsorted(timestamps, ts_ms, side="left")
    candidates = []
    if i < len(timestamps):
        candidates.append((abs(int(timestamps[i]) - ts_ms), prices[i]))
    if i > 0:
        candidates.append((abs(int(timestamps[i - 1]) - ts_ms), prices[i - 1]))
    return min(candidates, key=lambda c: c[0])[1]


def load_events(conn):
    return pd.read_sql_query(
        """
        SELECT wallet_address, instrument, asset, option_type, strike, expiry, side, size, price,
               index_price_usd, rfq_id, timestamp, notional_usd
        FROM options_events
        WHERE wallet_address IS NOT NULL
        """,
        conn,
    )


def simulate_copy_trades(df, now_ms):
    """Core simulation, reused by both the full-history backtest (this
    file's run()) and scripts/backtest_walkforward.py, which needs the
    same per-position outcomes split by resolution date instead of
    pre-aggregated by wallet. Returns (positions: list of dicts with
    wallet_address/instrument/resolved_ts/entry_side/return_pct,
    excluded_stale: dict, excluded_open: dict)."""
    # sort by timestamp first -- groupby(...).agg("first") on an unsorted
    # frame picks whatever row happens to be first in SQLite's return
    # order, not the chronologically first leg, which could silently pick
    # the wrong entry_side for a position with legs on both sides.
    df = df.sort_values("timestamp").copy()
    df["signed_qty"] = np.where(df["side"] == "buy", df["size"], -df["size"])

    grouped = df.groupby(["wallet_address", "instrument"]).agg(
        asset=("asset", "first"),
        option_type=("option_type", "first"),
        strike=("strike", "first"),
        expiry=("expiry", "first"),
        entry_ts=("timestamp", "min"),
        close_ts=("timestamp", "max"),
        entry_side=("side", "first"),
        net_qty=("signed_qty", "sum"),
    ).reset_index()
    grouped["expiry_ms"] = (grouped["expiry"] * 1000).astype("int64")
    grouped["expired"] = grouped["expiry_ms"] <= now_ms
    grouped["is_closed"] = grouped["net_qty"].abs() <= 1e-9

    price_index = build_instrument_price_index(df)
    spot_index = build_spot_price_index(df)
    own_rfqs = df.groupby(["wallet_address", "instrument"])["rfq_id"].apply(lambda s: set(s.dropna())).to_dict()

    positions = []
    excluded_stale = {}
    excluded_open = {}

    for row in grouped.itertuples():
        wallet = row.wallet_address
        if not row.is_closed and not row.expired:
            excluded_open[wallet] = excluded_open.get(wallet, 0) + 1
            continue

        excluded_rfqs = own_rfqs.get((wallet, row.instrument), set())
        entry_price, _ = next_other_wallet_print(price_index, row.instrument, wallet, row.entry_ts, excluded_rfqs)
        if entry_price is None or entry_price <= 0:
            excluded_stale[wallet] = excluded_stale.get(wallet, 0) + 1
            continue

        if row.is_closed:
            exit_value, resolved_ts = next_other_wallet_print(price_index, row.instrument, wallet, row.close_ts, excluded_rfqs)
            if exit_value is None:
                excluded_stale[wallet] = excluded_stale.get(wallet, 0) + 1
                continue
        else:  # expired, net_qty != 0 -- settle via intrinsic value at expiry
            spot = nearest_spot_price(spot_index, row.asset, row.expiry_ms)
            if spot is None:
                excluded_stale[wallet] = excluded_stale.get(wallet, 0) + 1
                continue
            exit_value = max(spot - row.strike, 0) if row.option_type == "call" else max(row.strike - spot, 0)
            resolved_ts = row.expiry_ms

        if row.entry_side == "buy":
            return_pct = (exit_value - entry_price) / entry_price
        else:
            return_pct = (entry_price - exit_value) / entry_price

        positions.append({
            "wallet_address": wallet,
            "instrument": row.instrument,
            "resolved_ts": int(resolved_ts),
            "entry_side": row.entry_side,
            "asset": row.asset,
            "option_type": row.option_type,
            "return_pct": return_pct,
            # additive -- purely exposing fields already computed above, no
            # change to any existing caller's behavior. Added for
            # scripts/backtest_stop_loss.py, which needs the actual price
            # level and position window to simulate a mid-life stop-loss.
            "entry_price": entry_price,
            "entry_ts": int(row.entry_ts),
            "strike": row.strike,
            "expiry_ms": int(row.expiry_ms),
        })

    return positions, excluded_stale, excluded_open


def aggregate_by_wallet(positions):
    """positions (list of dicts, see simulate_copy_trades) -> wallet_address
    -> list of return_pct. Shared aggregation step for both the
    full-history and per-period (walk-forward) rollups."""
    results = {}
    for p in positions:
        results.setdefault(p["wallet_address"], []).append(p["return_pct"])
    return results


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    t0 = time.time()
    df = load_events(conn)
    print(f"loaded {len(df)} rows in {time.time()-t0:.1f}s")

    now_ms = int(time.time() * 1000)
    t0 = time.time()
    positions, excluded_stale, excluded_open = simulate_copy_trades(df, now_ms)
    print(f"simulated {len(positions)} copy trades in {time.time()-t0:.1f}s")

    results = aggregate_by_wallet(positions)

    conn.execute("DELETE FROM copy_trade_backtest")
    now_ms2 = int(time.time() * 1000)
    rows_out = []
    for wallet, returns in results.items():
        n = len(returns)
        wins = sum(1 for r in returns if r > 0)
        losses = n - wins
        win_rate = wins / n if n else None
        wilson = wilson_lower_bound(wins, n)
        avg_return = sum(returns) / n
        median_return = float(np.median(returns))
        total_return = sum(returns)
        worst_return = min(returns)
        best_return = max(returns)
        rows_out.append((
            wallet, n, wins, losses, win_rate, wilson, avg_return, median_return, total_return,
            excluded_stale.get(wallet, 0), excluded_open.get(wallet, 0), worst_return, best_return, now_ms2,
        ))

    conn.executemany(
        """
        INSERT INTO copy_trade_backtest
            (wallet_address, copy_positions, copy_wins, copy_losses, copy_win_rate, copy_win_rate_wilson_low,
             copy_avg_return_pct, copy_median_return_pct, copy_total_return_pct, excluded_stale, excluded_open,
             copy_worst_return_pct, copy_best_return_pct, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_out,
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    print(f"wrote {len(rows_out)} copy_trade_backtest rows")
    conn.close()


if __name__ == "__main__":
    run()
