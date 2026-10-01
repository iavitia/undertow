"""Per-position entry-size ratio: is this wallet's entry into a new
position unusually large relative to ITS OWN trading history, regardless
of whether it's large by market standards?

Reuses the walk-forward principle analysis/anomaly_detection.py's
flag_wallet_jumps() already established (expanding log-notional mean of a
wallet's own PRIOR trades, shift(1) so nothing looks ahead) -- not the
function or its constants directly, since those were tuned for a
different purpose (market-mover-scale anomalies feeding the original
signals/news-correlation work: WALLET_JUMP_MULTIPLIER=8.0,
WALLET_JUMP_MIN_NOTIONAL_USD=200,000) and won't fire anywhere near the
1.3x-3.2x multiples found by hand in the blowup investigation (see
conversation: iron-grove-22, north-quarry-44, copper-current-1e,
faded-summit-12 all sized meaningfully above their own norm on their
worst losses; cedar-ridge-e9 and glacial-thicket-ce blew up at completely
ordinary size, so this is a real-but-partial signal, not universal).

Scoped to position-OPENING legs only (one row per (wallet_address,
instrument), matching how simulate_copy_trades and compute_wallet_positions
already define "entry"), not every fill the way flag_wallet_jumps
operates -- a wallet's "usual entry size" should be judged against other
entries, not diluted by however many partial fills happened to build up
each position."""
import numpy as np


def compute_entry_size_ratios(df, min_prior=10):
    """df: the same raw events DataFrame load_events() produces (needs
    wallet_address, instrument, timestamp, notional_usd, side). Returns a
    DataFrame indexed by (wallet_address, instrument) with columns
    entry_notional, entry_side, prior_count, size_ratio -- size_ratio is
    NaN wherever prior_count < min_prior (not enough of the wallet's own
    history yet to judge "unusual" -- same reasoning as the existing
    detector's own minimum-prior-trades floor)."""
    entries = (
        df.sort_values("timestamp")
        .groupby(["wallet_address", "instrument"], as_index=False)
        .agg(entry_notional=("notional_usd", "first"), entry_side=("side", "first"), entry_ts=("timestamp", "first"))
        .sort_values(["wallet_address", "entry_ts"])
    )
    entries["log_notional"] = np.log(entries["entry_notional"].clip(lower=1))
    grp = entries.groupby("wallet_address")["log_notional"]
    entries["prior_count"] = grp.cumcount()
    prior_mean_log = grp.transform(lambda s: s.expanding().mean().shift(1))
    entries["size_ratio"] = np.where(
        entries["prior_count"] >= min_prior,
        np.exp(entries["log_notional"] - prior_mean_log),
        np.nan,
    )
    return entries.set_index(["wallet_address", "instrument"])[["entry_notional", "entry_side", "prior_count", "size_ratio"]]
