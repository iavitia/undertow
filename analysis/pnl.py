"""Settlement-price logic shared between api/main.py's live wallet-portfolio
computation and scripts/resolve_paper_trades.py -- moved here (rather than
left duplicated) because this project already hit the "same math in two
places, they drift apart" bug once this session (see the value-timeline vs
portfolio P&L split that was fixed earlier), and the whole point of this
project's paper-trading track record is that it prices exits the same way
the backtest that put a wallet on the watchlist did.

Not shared with scripts/backtest_copy_trades.py's nearest_spot_price(): that
one is a numpy-vectorized version doing this same nearest-price lookup
across ~200k positions in a batch job, a genuinely different performance
context from the single-lookup, live-request uses here. Forcing them
through one SQL-per-call implementation would regress the batch job from
seconds to a very long time for no real simplification -- the formula
below (intrinsic_value) is the part actually worth sharing across both, and
is small enough that both already inline it identically; not worth the
indirection of extracting a two-line function twice-removed from its use.
"""


def nearest_index_price(conn, asset, ts_ms):
    """Stand-in for an options settlement price Derive's trade API doesn't
    expose: the index_price_usd of whichever trade (any wallet, same
    asset) is closest in time to ts_ms. Checks both directions separately
    so it can use the (asset, timestamp) index instead of scanning every
    row of that asset to sort by |timestamp - ts_ms|.

    Returns (price, gap_ms) -- gap_ms is how far the matched sample
    actually is from ts_ms, in milliseconds. Callers that persist their
    result permanently (scripts/resolve_paper_trades.py,
    scripts/run_watchlist_agent.py's check_wallet_outcomes) need this to
    avoid locking in a stale estimate just because it was the only one
    ingested yet -- see conversation: a settlement computed from a sample
    3h48m away from the real expiry moment silently produced a wrong
    outcome for a naked short (BTC-20260825-80000-C, id=163) when the
    correct nearest sample, ingested a few ticks later, was 2.5 minutes
    away and told the opposite story. Callers that recompute live on every
    request (api/main.py's compute_wallet_positions) can ignore gap_ms --
    they always reflect the current best-available sample, nothing to lock in."""
    before = conn.execute(
        "SELECT index_price_usd, timestamp FROM options_events WHERE asset = ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
        (asset, ts_ms),
    ).fetchone()
    after = conn.execute(
        "SELECT index_price_usd, timestamp FROM options_events WHERE asset = ? AND timestamp >= ? ORDER BY timestamp ASC LIMIT 1",
        (asset, ts_ms),
    ).fetchone()
    candidates = [r for r in (before, after) if r is not None]
    if not candidates:
        return None, None
    best = min(candidates, key=lambda r: abs(r["timestamp"] - ts_ms))
    return best["index_price_usd"], abs(best["timestamp"] - ts_ms)


def intrinsic_value(spot, strike, option_type):
    """European cash-settlement value at expiry: what a call/put is worth
    at a given spot price, ignoring premium already paid/received."""
    return max(spot - strike, 0) if option_type == "call" else max(strike - spot, 0)
