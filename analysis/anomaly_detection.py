import numpy as np
import pandas as pd

from config import (
    SKEW_ZSCORE_THRESHOLD,
    TRADE_SIZE_ZSCORE_THRESHOLD,
    VOLUME_ZSCORE_THRESHOLD,
    WALLET_JUMP_MAX_PRIOR_STD_LOG,
    WALLET_JUMP_MIN_NOTIONAL_USD,
    WALLET_JUMP_MIN_PRIOR_TRADES,
    WALLET_JUMP_MULTIPLIER,
)


def load_trades(conn, asset):
    df = pd.read_sql_query(
        """
        SELECT id, asset, timestamp, notional_usd, option_type, side, instrument,
               wallet_address, source_trade_id
        FROM options_events
        WHERE source = 'derive' AND asset = ?
        """,
        conn,
        params=(asset,),
    )
    return _add_derived_columns(df)


def load_all_trades(conn):
    """Both assets together, for the wallet-jump detector -- a wallet's
    'usual size' is about its own behavior, not siloed by asset."""
    df = pd.read_sql_query(
        """
        SELECT id, asset, timestamp, notional_usd, option_type, side, instrument,
               wallet_address, source_trade_id
        FROM options_events
        WHERE source = 'derive'
        """,
        conn,
    )
    return _add_derived_columns(df)


def _add_derived_columns(df):
    df["day"] = pd.to_datetime(df["timestamp"], unit="ms").dt.floor("D")
    df["dow"] = df["day"].dt.dayofweek  # 0=Monday .. 6=Sunday
    df["log_notional"] = np.log(df["notional_usd"].clip(lower=1))
    return df


def flag_large_trades(df):
    """Per-trade size anomaly: z-score of log(notional) within its own
    day-of-week cohort, against a static baseline built from the whole
    backfilled corpus. Log-transformed because trade size is heavy-tailed --
    a few whale trades would otherwise blow out a raw mean/std and mask
    everything but the most extreme outliers."""
    baseline = df.groupby("dow")["log_notional"].agg(["mean", "std"])
    df = df.join(baseline, on="dow", rsuffix="_baseline")
    df["size_zscore"] = (df["log_notional"] - df["mean"]) / df["std"]
    df["is_large_trade"] = df["size_zscore"] >= TRADE_SIZE_ZSCORE_THRESHOLD
    return df


def flag_wallet_jumps(df):
    """Second detector: a wallet trading far bigger than ITS OWN history,
    regardless of whether the trade is large by market standards. Catches
    the 'usually quiet wallet suddenly does something large' pattern that
    the market-wide size detector misses -- a wallet whose typical trade is
    $80k doing a $500k trade won't clear a market-wide size z-score, but is
    a 6x jump for that wallet specifically.

    Uses an expanding walk-forward baseline (only trades strictly before
    the one being evaluated) rather than the whole-corpus static baseline
    flag_large_trades uses, since 'usually quiet' is inherently about trade
    order -- a static baseline would leak future trades into a wallet's
    'usual' size."""
    df = df.sort_values(["wallet_address", "timestamp"]).copy()
    grp = df.groupby("wallet_address")["log_notional"]
    df["wallet_prior_count"] = grp.cumcount()
    df["wallet_prior_mean_log"] = grp.transform(lambda s: s.expanding().mean().shift(1))
    df["wallet_prior_std_log"] = grp.transform(lambda s: s.expanding().std().shift(1))
    df["wallet_jump_ratio"] = np.exp(df["log_notional"] - df["wallet_prior_mean_log"])
    df["is_wallet_jump"] = (
        (df["wallet_prior_count"] >= WALLET_JUMP_MIN_PRIOR_TRADES)
        & (df["wallet_jump_ratio"] >= WALLET_JUMP_MULTIPLIER)
        & (df["notional_usd"] >= WALLET_JUMP_MIN_NOTIONAL_USD)
        & (df["wallet_prior_std_log"] <= WALLET_JUMP_MAX_PRIOR_STD_LOG)
    )
    return df


def compute_daily_signals(df):
    """Daily volume-spike and put/call skew-shift signals, baselined per
    day-of-week (weekends run ~40-45% below weekday volume, confirmed in
    the Phase 1 sanity check -- a flat baseline would flag every
    Friday-vs-Sunday swing as 'anomalous')."""
    df = df.copy()
    df["call_notional"] = df["notional_usd"].where(df["option_type"] == "call", 0.0)
    df["put_notional"] = df["notional_usd"].where(df["option_type"] == "put", 0.0)

    daily = df.groupby("day").agg(
        total_notional=("notional_usd", "sum"),
        trade_count=("notional_usd", "size"),
        call_notional=("call_notional", "sum"),
        put_notional=("put_notional", "sum"),
    )
    daily["dow"] = pd.to_datetime(daily.index).dayofweek
    daily["skew"] = daily["call_notional"] / (daily["call_notional"] + daily["put_notional"])

    for col, prefix in (("total_notional", "vol"), ("skew", "skew")):
        stats = daily.groupby("dow")[col].agg(["mean", "std"])
        daily[f"{prefix}_mean"] = daily["dow"].map(stats["mean"])
        daily[f"{prefix}_std"] = daily["dow"].map(stats["std"])

    daily["volume_zscore"] = (daily["total_notional"] - daily["vol_mean"]) / daily["vol_std"]
    daily["skew_zscore"] = (daily["skew"] - daily["skew_mean"]) / daily["skew_std"]
    daily["is_volume_spike"] = daily["volume_zscore"] >= VOLUME_ZSCORE_THRESHOLD
    daily["is_skew_shift"] = daily["skew_zscore"].abs() >= SKEW_ZSCORE_THRESHOLD

    return daily.reset_index()
