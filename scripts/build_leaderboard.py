import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH

# Heuristic for separating automated market-making from discretionary
# trading: many small fills per position, or very high daily trade
# frequency, both point to a bot rather than a human placing a handful of
# directional bets. Thresholds picked by inspection (see conversation) --
# genuine directional traders in this dataset cluster under 8 legs/position
# and 15 legs/day; known market-maker wallets run into the hundreds.
LEGS_PER_POSITION_BOT_THRESHOLD = 8
LEGS_PER_DAY_BOT_THRESHOLD = 15


def compute_positions(df, now_ms):
    """Per (wallet, instrument) P&L. Derive's realized_pnl on a trade leg
    is only ever non-zero when that leg closes an existing position -- a
    wallet that sells to open and never buys back (or buys to open and
    never sells/exercises) gets realized_pnl=0 on every leg forever, even
    once the option has actually expired and settled. Cash-flow
    conservation means total position P&L = sum(all leg premiums, sells
    positive/buys negative) + settlement value of whatever's left open at
    expiry - fees; for a fully-closed position (net_qty==0) this was
    verified against Derive's own realized_pnl sum and matches exactly
    (see conversation), so it's used unconditionally once an instrument
    has expired rather than trusting realized_pnl. The settlement price is
    approximated as the nearest available index_price_usd (any wallet's
    trades, same asset) to the expiry timestamp, since Derive's trade API
    doesn't expose the actual settlement price."""
    df = df.copy()
    df["signed_qty"] = np.where(df["side"] == "buy", df["size"], -df["size"])
    df["cashflow"] = np.where(df["side"] == "sell", df["price"] * df["size"], -df["price"] * df["size"])

    per_position = df.groupby(["wallet_address", "instrument"]).agg(
        asset=("asset", "first"),
        option_type=("option_type", "first"),
        strike=("strike", "first"),
        expiry=("expiry", "first"),
        net_qty=("signed_qty", "sum"),
        cashflow=("cashflow", "sum"),
        fees=("trade_fee", "sum"),
        legs=("instrument", "size"),
        derive_pnl_sum=("realized_pnl", "sum"),
    ).reset_index()

    per_position["expiry_ms"] = (per_position["expiry"] * 1000).astype("int64")
    per_position["expired"] = per_position["expiry_ms"] <= now_ms

    # nearest index price per (asset, expiry) via merge_asof -- needs both
    # frames sorted by the asof key, done per-asset since merge_asof only
    # matches within groups when the "by" columns match exactly.
    settled = []
    for asset, grp in per_position.groupby("asset"):
        price_series = (
            df.loc[df["asset"] == asset, ["timestamp", "index_price_usd"]]
            .dropna()
            .sort_values("timestamp")
        )
        grp_sorted = grp.sort_values("expiry_ms")
        if price_series.empty:
            grp_sorted["index_price_usd"] = np.nan
        else:
            grp_sorted = pd.merge_asof(
                grp_sorted, price_series, left_on="expiry_ms", right_on="timestamp", direction="nearest"
            )
        settled.append(grp_sorted)
    per_position = pd.concat(settled, ignore_index=True)

    intrinsic = np.where(
        per_position["option_type"] == "call",
        np.maximum(per_position["index_price_usd"] - per_position["strike"], 0),
        np.maximum(per_position["strike"] - per_position["index_price_usd"], 0),
    )
    has_settlement_price = per_position["index_price_usd"].notna()
    needs_estimate = per_position["expired"] & (per_position["net_qty"].abs() > 1e-9) & has_settlement_price
    settlement_cashflow = np.where(needs_estimate, per_position["net_qty"] * intrinsic, 0.0)

    expired_and_priced = per_position["expired"] & (has_settlement_price | (per_position["net_qty"].abs() <= 1e-9))
    per_position["net_pnl"] = np.where(
        expired_and_priced,
        per_position["cashflow"] + settlement_cashflow - per_position["fees"],
        per_position["derive_pnl_sum"],
    )
    per_position["is_resolved"] = np.where(expired_and_priced, True, per_position["derive_pnl_sum"].abs() > 1e-6)
    per_position["is_win"] = per_position["is_resolved"] & (per_position["net_pnl"] > 1e-6)
    per_position["is_loss"] = per_position["is_resolved"] & (per_position["net_pnl"] < -1e-6)
    per_position = per_position.rename(columns={"fees": "fees_position"})
    return per_position[["wallet_address", "instrument", "net_pnl", "fees_position", "legs", "is_resolved", "is_win", "is_loss"]]


def run():
    conn = sqlite3.connect(DB_PATH)

    t0 = time.time()
    df = pd.read_sql_query(
        """
        SELECT wallet_address, instrument, asset, option_type, strike, expiry, side, size, price,
               notional_usd, index_price_usd, realized_pnl, trade_fee, timestamp
        FROM options_events
        WHERE wallet_address IS NOT NULL
        """,
        conn,
    )
    # Defensive: ~18.6% of rows (ETH/BTC, pre-existing, surfaced while
    # adding SOL -- see conversation) have realized_pnl stored as a
    # UUID-shaped TEXT value instead of a number, root cause not yet
    # investigated. Left as-is, this makes the whole column object-dtype
    # and turns groupby(...).sum() into silent STRING CONCATENATION for
    # affected groups instead of addition, which is worse than just
    # excluding the bad values -- coerce to numeric, treating anything
    # unparseable as missing (matches how a genuinely-null realized_pnl is
    # already handled elsewhere in this pipeline).
    n_bad = df["realized_pnl"].apply(lambda v: isinstance(v, str)).sum()
    if n_bad:
        print(f"WARNING: {n_bad} rows have non-numeric realized_pnl (see conversation) -- coercing to NaN")
    df["realized_pnl"] = pd.to_numeric(df["realized_pnl"], errors="coerce")
    print(f"loaded {len(df)} rows in {time.time()-t0:.1f}s")

    now_ms = int(time.time() * 1000)
    per_position = compute_positions(df, now_ms).rename(columns={"fees_position": "fees"})

    agg = per_position.groupby("wallet_address").agg(
        positions=("instrument", "count"),
        resolved=("is_resolved", "sum"),
        wins=("is_win", "sum"),
        losses=("is_loss", "sum"),
        total_pnl=("net_pnl", "sum"),
        total_fees=("fees", "sum"),
        total_legs=("legs", "sum"),
    ).reset_index()
    agg["net_of_fees"] = agg["total_pnl"] - agg["total_fees"]
    agg["win_rate"] = agg["wins"] / agg["resolved"]
    agg["legs_per_position"] = agg["total_legs"] / agg["positions"]

    timing = df.groupby("wallet_address")["timestamp"].agg(["min", "max"]).reset_index()
    timing.columns = ["wallet_address", "first_ts", "last_ts"]
    timing["active_days"] = ((timing["last_ts"] - timing["first_ts"]) / 86400000).round().astype(int) + 1
    agg = agg.merge(timing, on="wallet_address")
    agg["legs_per_day"] = agg["total_legs"] / agg["active_days"]
    agg["is_likely_bot"] = (
        (agg["legs_per_position"] > LEGS_PER_POSITION_BOT_THRESHOLD)
        | (agg["legs_per_day"] > LEGS_PER_DAY_BOT_THRESHOLD)
    )

    # trading-pattern breakdown
    df["is_call"] = df["option_type"] == "call"
    df["is_buy"] = df["side"] == "buy"
    df["is_btc"] = df["asset"] == "BTC"
    pattern = df.groupby("wallet_address").agg(
        call_pct=("is_call", "mean"),
        buy_pct=("is_buy", "mean"),
        btc_pct=("is_btc", "mean"),
        avg_notional_usd=("notional_usd", "mean"),
    ).reset_index()
    agg = agg.merge(pattern, on="wallet_address")

    conn.execute("DELETE FROM wallet_leaderboard")
    now_ms = int(time.time() * 1000)
    rows = [
        (
            r.wallet_address, int(r.positions), int(r.resolved), int(r.wins), int(r.losses),
            None if pd.isna(r.win_rate) else float(r.win_rate),
            float(r.total_pnl), float(r.total_fees), float(r.net_of_fees), int(r.total_legs),
            float(r.legs_per_position), int(r.active_days), float(r.legs_per_day), int(r.is_likely_bot),
            float(r.call_pct), float(r.buy_pct), float(r.btc_pct), float(r.avg_notional_usd),
            int(r.first_ts), int(r.last_ts), now_ms,
        )
        for r in agg.itertuples()
    ]
    conn.executemany(
        """
        INSERT INTO wallet_leaderboard
            (wallet_address, positions, resolved, wins, losses, win_rate, total_pnl, total_fees,
             net_of_fees, total_legs, legs_per_position, active_days, legs_per_day, is_likely_bot,
             call_pct, buy_pct, btc_pct, avg_notional_usd, first_ts, last_ts, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    print(f"wrote {len(rows)} wallet leaderboard rows")
    conn.close()


if __name__ == "__main__":
    run()
