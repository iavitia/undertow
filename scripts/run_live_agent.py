"""Phase 4: one tick of REAL (testnet) order placement, mirroring the exact
same watchlist wallets and mirror-rule/weak-edge-gate logic
scripts/run_watchlist_agent.py uses for paper trading -- via the shared
find_new_candidates() it now exposes. The two agents run independently
(see db/schema.sql's watchlist.live_last_checked_ts comment): this script
owns its own cursor, so it sees every real candidate regardless of when
the paper-trading agent last ran, and vice versa.

Gated entirely behind config.EXECUTION_ENABLED (default false) --
refuses to place a single order unless explicitly turned on. Not wired
into a scheduled task -- manual invocation only while this is being
proven out, so a human is in the loop for every run.

Real, structural limitation, not an oversight: the watchlist wallets we
mirror trade on Derive MAINNET (clients/derive_client.py's
DERIVE_BASE_URL), but this places orders on Derive TESTNET
(clients/derive_execution_client.py) -- a separate market with its own
instrument set, liquidity, and pricing. A mainnet instrument name isn't
guaranteed to exist on testnet at all, and even when it does, testnet's
own live price is what we trade at -- not whatever the source wallet
actually paid on mainnet. This script can only mirror the SIGNAL (which
wallet, which instrument, which side) as closely as two separate markets
allow; candidates whose instrument isn't listed on testnet are skipped
and logged, not forced through.

Real orders are priced at testnet's own current mark price (the same
price a copier watching the book would reasonably use), not the
next-other-wallet-print methodology scripts/run_watchlist_agent.py uses
for paper trading -- that methodology exists to backtest-validate a
*simulated* fill against real historical prints; a real order doesn't
need to wait for one, it just goes on the book.

Settlement for testnet_order rows currently rides on the exact same
scripts/resolve_paper_trades.py used for simulated rows (it doesn't
branch on mode) -- which settles against MAINNET index price data, not
testnet's own. That means a testnet_order row's eventual return_pct
reflects what the position would have been worth on mainnet at that
strike, not testnet's own real settlement. Acceptable for now since the
capital at risk is nominal testnet money and the goal is proving the
mechanics -- a real-money version would need testnet's own settlement
price, not this project's existing mainnet-sourced one."""
import sys
import time
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.derive_execution_client import (
    estimate_fee,
    get_account_state,
    get_instrument,
    get_ticker,
    place_order,
    simulate_margin,
)
from config import DERIVE_SUBACCOUNT_ID, DERIVE_SUBACCOUNT_ID_FAST, DERIVE_SUBACCOUNT_ID_SOL, EXECUTION_ENABLED
from db.cloud_conn import INTEGRITY_ERRORS, get_live_conn
from scripts.run_watchlist_agent import find_new_candidates

# All three assets, both sides, are eligible for real execution -- BTC/ETH
# naked-short margin ($100-300+/contract against $5-25 premium, confirmed
# via real testnet orders) used to rule BTC/ETH sells out entirely, but
# that was a blunt per-asset carve-out standing in for the real concern:
# margin eating too much of the account. The per-config
# max_margin_utilization_fraction below replaces it with the actual
# constraint (see conversation) --
# every order, any asset/side, is pre-checked against it, so BTC/ETH
# sells are allowed through but capped by cost rather than excluded by
# asset.
LIVE_ASSETS = ("SOL", "BTC", "ETH")

# How many days out a candidate's expiry can be and still qualify for the
# "fast" subaccount below -- see conversation: a small account needs to
# turn capital over quickly and prioritize whatever's most time-critical,
# not sit tied up in a handful of long-dated positions.
FAST_MAX_DAYS_TO_EXPIRY = 7

# V3's risk universes split BTC/ETH (Prime) from SOL (Alt) -- they can't
# share a subaccount (see conversation), so each asset routes to its own,
# financially-independent subaccount/margin pool. Every per-subaccount
# check below (margin cap, buying-power floor) runs separately per
# subaccount_id, not pooled across both.
#
# "fast" is a third, deliberately small-capital subaccount that runs the
# *same* signal as prime/alt in parallel, not instead of them -- added
# specifically to observe real margin/fee/scheduling-delay economics
# under tight capital (see conversation), which prime/alt's larger
# balances don't meaningfully stress. Filtered to short-expiry
# candidates only (FAST_MAX_DAYS_TO_EXPIRY) and processed soonest-expiry
# first, so its tiny margin budget goes to whatever's most time-critical
# instead of whatever happened to arrive first in find_new_candidates()'s
# order. Only runs once DERIVE_SUBACCOUNT_ID_FAST is actually set --
# until then this config is silently absent, same as any other unset
# subaccount.
#
# Needing the SAME candidate to produce a real order on more than one of
# these subaccounts (e.g. a short-dated BTC candidate going to both prime
# and fast) is exactly what paper_trades.subaccount_id/the composite
# UNIQUE(source_event_id, subaccount_id) constraint exists for -- see
# conversation and db/schema.sql's comment on that column. Before that
# migration, a second real order for a candidate prime/alt already took
# would have hit the old UNIQUE(source_event_id) constraint and been
# silently swallowed by `except INTEGRITY_ERRORS: pass`, leaving a real
# exchange position with no DB record at all.
# Standing portfolio-level cap, not a per-tick one: total margin currently
# committed across every real open position on a subaccount must never
# exceed this fraction of that subaccount's own total value
# (collaterals_initial_margin), checked fresh before every single order
# via a margin simulation (see _margin_utilization_after and
# conversation -- placing real orders to probe margin risks them
# actually filling on testnet's thin, one-sided books, which happened
# and left stray positions needing manual cleanup; simulate_margin()
# never touches the real order book). Per-config, not a single constant
# -- prime/alt run at 0.5 (comfortable headroom on their larger
# balances), fast runs tighter at 0.3 specifically because its $100
# balance makes liquidation a real concern in a way the larger accounts
# don't have (see conversation).
PRIME_MAX_MARGIN_UTILIZATION_FRACTION = 0.5
FAST_MAX_MARGIN_UTILIZATION_FRACTION = 0.3

SUBACCOUNT_CONFIGS = [
    cfg for cfg in [
        {"name": "prime", "subaccount_id": DERIVE_SUBACCOUNT_ID, "assets": {"BTC", "ETH"}, "max_days_to_expiry": None, "max_margin_utilization_fraction": PRIME_MAX_MARGIN_UTILIZATION_FRACTION},
        {"name": "alt", "subaccount_id": DERIVE_SUBACCOUNT_ID_SOL, "assets": {"SOL"}, "max_days_to_expiry": None, "max_margin_utilization_fraction": PRIME_MAX_MARGIN_UTILIZATION_FRACTION},
        # Prime-universe subaccount (see conversation) -- BTC/ETH only,
        # same restriction as the main prime config. A SOL candidate
        # would just fail with "not in portfolio's risk universe" against
        # it, same as it would against prime -- scoping the asset set
        # correctly here avoids that noise rather than relying on
        # place_order()'s try/except to paper over it.
        {"name": "fast", "subaccount_id": DERIVE_SUBACCOUNT_ID_FAST, "assets": {"BTC", "ETH"}, "max_days_to_expiry": FAST_MAX_DAYS_TO_EXPIRY, "max_margin_utilization_fraction": FAST_MAX_MARGIN_UTILIZATION_FRACTION},
    ]
    if cfg["subaccount_id"] is not None
]

# Premium must clear this multiple of the estimated fee before a real
# order gets placed -- see conversation: three real orders this session
# each paid $1.50-1.60 in flat fees against $0.02-0.91 in premium, a
# guaranteed loss before settlement even happens. 5x is a starting point
# (premium should be a healthy multiple of the fee, not just barely clear
# it), not yet calibrated against a real run.
MIN_FEE_COVERAGE_MULTIPLE = 5
# How far above the instrument's own minimum_amount this will size up to
# clear the fee bar, as a multiple of minimum_amount -- caps runaway
# sizing (and the margin that comes with it) rather than scaling
# indefinitely for an instrument whose premium is just very thin.
MAX_SIZE_MULTIPLE = 20
# Stop placing further real orders partway through a tick once buying
# power has fallen under this fraction of what it was at the tick's
# start -- a simple backstop against one tick overcommitting the account,
# independent of and in addition to per-trade sizing.
MIN_BUYING_POWER_FRACTION = 0.5

# Loss-limiting for real naked shorts -- see conversation. The old live
# stop-loss engine (config.STOP_MULTIPLE = 1.10, a 10% loss trigger) only
# ever protected PAPER trades via a 15-minute poll, and was retired after
# a real overshoot (entry $2.80 -> exit $28.20, a 10x move caught a full
# tick late -- the poll simply couldn't keep up). This uses Derive's own
# native, exchange-side trigger orders instead (confirmed live this
# session: works on an OPTIONS instrument, not just perps -- Derive's own
# matching engine fires it, no polling on our end at all). 1.5x (a 50%
# loss of the premium collected) was chosen deliberately looser than the
# old 1.10x: the historical backtest (scripts/analyze_stop_sensitivity.py)
# finds tighter always "wins" on raw dollars with no interior peak, but
# that backtest prices off bare intrinsic value with zero IV modeling --
# real IV whipsaw is exactly what destroyed the old tight threshold in
# production, and a native trigger fixes the *catching-it-late* problem,
# not the *whipsaw* problem. 1.5x gives real daily crypto volatility room
# to breathe without chasing a backtest number that was never measuring
# the risk that actually matters here.
STOP_LOSS_MULTIPLE = 1.5
# The triggered order's own limit price, as a further multiple above the
# trigger -- once fired, a trigger order still has to clear the book like
# any limit order, and pricing it exactly at the trigger risks it failing
# to fill if the market gaps past that level before matching. Still
# bounds worst-case realized loss well under "no stop at all" (87.5% of
# premium vs. the no-stop floor's 100%), while generous enough to
# reliably fill near the intended trigger.
STOP_LOSS_LIMIT_BUFFER = 0.25
# Derive-confirmed live bounds on a trigger order's signature_expiry_sec
# (a plain order's usual 10-minute window gets rejected outright for one)
# -- a trigger order can sit dormant for a while before firing, so its
# signature has to outlive that.
MIN_TRIGGER_SIGNATURE_WINDOW_SEC = 51877
MAX_TRIGGER_SIGNATURE_WINDOW_SEC = 7776000


def _attach_stop_loss(instrument_name, subaccount_id, entry_price, amount, expiry_sec, now_sec):
    """Places the companion exchange-side stoploss trigger order for a
    just-opened real naked short -- see STOP_LOSS_MULTIPLE's comment.
    reduce_only=True so a stop that ever fires after the position's
    already closed some other way safely no-ops instead of opening a new
    long. Returns the stop order's id, or None if the position expires
    too soon to even get a valid trigger signature window (see
    MIN_TRIGGER_SIGNATURE_WINDOW_SEC) -- logged either way, never silently
    dropped, since an entry placed without its stop is a real gap, not a
    routine skip."""
    window = min(expiry_sec, now_sec + MAX_TRIGGER_SIGNATURE_WINDOW_SEC) - now_sec
    if window < MIN_TRIGGER_SIGNATURE_WINDOW_SEC:
        print(f"  {instrument_name} expires in {window / 3600:.1f}h -- too soon for a trigger order's minimum signature window, left UNPROTECTED")
        return None

    trigger_price = STOP_LOSS_MULTIPLE * entry_price
    stop_limit_price = trigger_price * (1 + STOP_LOSS_LIMIT_BUFFER)
    try:
        stop_result = place_order(
            instrument_name=instrument_name,
            direction="buy",
            amount=amount,
            limit_price=stop_limit_price,
            subaccount_id=subaccount_id,
            reduce_only=True,
            trigger_type="stoploss",
            trigger_price=trigger_price,
            trigger_price_type="mark",
            signature_expiry_sec=now_sec + window,
        )
        stop_order_id = stop_result["order"]["order_id"]
        print(f"  attached stop-loss {stop_order_id} for {instrument_name} -- triggers at mark>=${trigger_price:.2f} ({STOP_LOSS_MULTIPLE}x entry)")
        return stop_order_id
    except Exception as e:
        print(f"  WARNING: stop-loss attach FAILED for {instrument_name} -- position is UNPROTECTED: {e}")
        return None


def _size_for_fee_coverage(instrument, limit_price, index_price):
    """Finds the smallest order size (respecting amount_step, floored at
    minimum_amount) where premium collected clears
    MIN_FEE_COVERAGE_MULTIPLE times the estimated fee -- see conversation
    and clients/derive_execution_client.estimate_fee.

    Solved exactly rather than searched for: estimate_fee is
    base_fee + taker_fee_rate * amount * index_price (flat plus linear in
    amount) and premium is limit_price * amount (also linear, zero
    intercept), so premium >= N * fee reduces to one linear inequality in
    amount with a closed-form minimum -- no need to grope for it.
    A prior version searched by doubling (minimum, 2x, 4x, ...), which
    could overshoot this true minimum by nearly a full doubling step and
    needlessly inflate the size (and therefore the margin) a later
    cap check has to clear; confirmed this cost real candidates on the
    Fast subaccount that a precisely-sized order would have cleared (see
    conversation). Since margin scales linearly with size, this exact
    minimum is also the minimum-margin economical size -- there's no
    smaller size worth trying after this one.

    Returns None if the per-unit fee rate alone consumes the entire
    premium regardless of size (uneconomical at any size, not a rounding
    issue), or if even this true minimum exceeds MAX_SIZE_MULTIPLE times
    the instrument's minimum_amount (runaway size on a thin-premium
    instrument, same guard rail the old search had)."""
    base_fee = float(instrument["base_fee"])
    taker_fee_rate = float(instrument["taker_fee_rate"])
    amount_step = Decimal(instrument["amount_step"])
    minimum = Decimal(instrument["minimum_amount"])

    coefficient = limit_price - MIN_FEE_COVERAGE_MULTIPLE * taker_fee_rate * index_price
    if coefficient <= 0:
        return None  # fee's rate component alone eats the whole premium, at any size

    exact_minimum = (MIN_FEE_COVERAGE_MULTIPLE * base_fee) / coefficient
    amount = (Decimal(str(exact_minimum)) / amount_step).to_integral_value(rounding=ROUND_CEILING) * amount_step
    if amount < minimum:
        amount = minimum

    # Confirm the rounded amount actually clears the bar rather than
    # trusting the arithmetic blindly -- float/Decimal rounding could in
    # principle leave it one step short.
    premium = float(amount) * limit_price
    fee = estimate_fee(instrument, amount, index_price)
    if premium < MIN_FEE_COVERAGE_MULTIPLE * fee:
        amount += amount_step

    if amount > minimum * MAX_SIZE_MULTIPLE:
        return None
    return amount


def _margin_for_instrument(state, instrument_name):
    """Pulls this instrument's own margin figure out of a get_account_state()
    response's positions list (see conversation: this is where a resting
    order's margin impact actually shows up, as a negative
    open_orders_margin on the matching position row -- the top-level
    collateral summary was observed NOT to reflect a same-tick resting
    order, only settled positions)."""
    for p in state.get("positions", []):
        if p.get("instrument_name") == instrument_name:
            oom = p.get("open_orders_margin")
            return abs(float(oom)) if oom is not None else None
    return None


def _margin_utilization_after(state, new_instrument_name, new_side, new_amount, asset):
    """What fraction of total account value (collaterals_initial_margin)
    would be committed as margin if this candidate were added on top of
    every real position currently open, per simulate_margin(). Checked
    fresh before every order (not just once per tick) against the
    calling config's own max_margin_utilization_fraction -- the standing
    portfolio-level cap that replaced the old SOL-only/BTC-ETH-buy-only
    asset carve-out (see conversation).

    asset picks the right simulate_margin() mode: "SM" (no market) for
    BTC/ETH, which live in the Prime universe this endpoint defaults to --
    "PM2"+market="SOL" for SOL, confirmed the only way this public
    endpoint can price an Alt-universe instrument at all, even though the
    real SOL subaccount is itself Standard Margin (see
    simulate_margin's docstring for why that's a safe, conservative
    stand-in rather than an exact match)."""
    simulated_positions = {}
    for p in state.get("positions", []):
        amt = float(p.get("amount") or 0)
        if amt != 0:
            simulated_positions[p["instrument_name"]] = amt
    delta = float(new_amount) if new_side == "buy" else -float(new_amount)
    simulated_positions[new_instrument_name] = simulated_positions.get(new_instrument_name, 0.0) + delta

    total_value = float(state.get("collaterals_initial_margin") or 0)
    if total_value <= 0:
        return 1.0  # no collateral at all -- treat as fully committed, refuse

    margin_type, market = ("PM2", "SOL") if asset == "SOL" else ("SM", None)
    result = simulate_margin(
        simulated_positions=[{"instrument_name": k, "amount": str(v)} for k, v in simulated_positions.items()],
        simulated_collaterals=[
            {"asset_name": c["asset_name"], "amount": c["amount"]} for c in state.get("collaterals", [])
        ],
        margin_type=margin_type,
        market=market,
    )
    margin_used = total_value - float(result["post_initial_margin"])
    return margin_used / total_value


def run_live_tick(conn):
    """Real-order equivalent of detect_new_positions(): classifies new
    candidates the same way, but for anything that would be a pending
    entry, tries to place a real testnet order on every subaccount config
    it matches (see SUBACCOUNT_CONFIGS -- prime/alt are asset-routed and
    unfiltered, fast adds its own short-expiry filter on top and can run
    the same candidate alongside them), sized to clear a real fee
    multiple rather than always trading at instrument minimum (see
    _size_for_fee_coverage), checked against that subaccount's own
    standing margin cap before every order (see
    _margin_utilization_after and each config's
    max_margin_utilization_fraction), and stops placing new orders on a given subaccount
    partway through a tick if its buying power has dropped too far within
    just this tick (see MIN_BUYING_POWER_FRACTION, a separate,
    complementary guard, also per subaccount). Returns (placed, skipped_weak_edge,
    skipped_wrong_asset, skipped_uneconomical, skipped_untradeable,
    skipped_margin_cap)."""
    now_ms = int(time.time() * 1000)
    placed = 0
    skipped_weak_edge = 0
    skipped_wrong_asset = 0
    skipped_uneconomical = 0
    skipped_untradeable = 0
    skipped_margin_cap = 0

    # "Buying power" isn't a literal field -- confirmed live this session
    # the closest equivalent is the account's net initial_margin
    # (collaterals_initial_margin + positions_initial_margin, the latter
    # negative), i.e. remaining margin capacity after currently-open
    # positions. Shrinks as more real orders get placed, which is exactly
    # what this floor is meant to track. Tracked per subaccount_id (not
    # pooled) since every subaccount in SUBACCOUNT_CONFIGS is its own,
    # independently margined account.
    subaccount_ids = {cfg["subaccount_id"] for cfg in SUBACCOUNT_CONFIGS}
    current_states = {sid: get_account_state(sid) for sid in subaccount_ids}
    buying_power_floors = {
        sid: float(state.get("initial_margin") or 0) * MIN_BUYING_POWER_FRACTION
        for sid, state in current_states.items()
    }
    low_buying_power = {sid: False for sid in subaccount_ids}

    # find_new_candidates() already fully materializes its result (and
    # commits the watchlist cursor advance) before returning -- safe to
    # call exactly once here and reuse the list across every subaccount
    # config below, rather than re-querying per config (which would also
    # be wrong: a second call would see nothing new, the cursor having
    # already moved past everything on the first).
    pending = []
    for wallet, c, status, edge in find_new_candidates(conn, cursor_column="live_last_checked_ts"):
        if c["asset"] not in LIVE_ASSETS:
            skipped_wrong_asset += 1
            continue

        if status == "skipped_weak_edge":
            # Classification is per-candidate (the source wallet's own
            # track record), identical regardless of which subaccount(s)
            # would have taken it -- logged once here, not once per
            # matching config below.
            try:
                conn.execute(
                    """
                    INSERT INTO paper_trades
                        (source_wallet_address, source_event_id, instrument, asset, option_type, strike, expiry,
                         side, mode, intended_price, fill_status,
                         edge_bucket_n, edge_bucket_win_rate, edge_bucket_wilson_low, edge_bucket_median_return_pct,
                         created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'testnet_order', ?, 'skipped_weak_edge', ?, ?, ?, ?, ?)
                    """,
                    (wallet, c["id"], c["instrument"], c["asset"], c["option_type"], c["strike"], c["expiry"],
                     c["side"], c["price"],
                     edge["n"], edge["win_rate"], edge["wilson_low"], edge["median_return_pct"],
                     now_ms),
                )
                skipped_weak_edge += 1
            except INTEGRITY_ERRORS:
                pass
            continue

        pending.append((wallet, c, status, edge))

    # status == "pending_entry" from here -- each candidate tried against
    # every subaccount config whose asset/expiry filter it matches (see
    # SUBACCOUNT_CONFIGS), not just one. The composite
    # UNIQUE(source_event_id, subaccount_id) constraint (see
    # db/schema.sql) is what makes a candidate landing a real order on
    # more than one subaccount safe to record.
    for config in SUBACCOUNT_CONFIGS:
        subaccount_id = config["subaccount_id"]
        max_days = config["max_days_to_expiry"]
        matching = [
            item for item in pending
            if item[1]["asset"] in config["assets"]
            and (max_days is None or (item[1]["expiry"] - now_ms / 1000) / 86400 <= max_days)
        ]
        if max_days is not None:
            matching.sort(key=lambda item: item[1]["expiry"])  # soonest-expiry first

        for wallet, c, status, edge in matching:
            if low_buying_power[subaccount_id]:
                print(f"{c['instrument']} skipped on {config['name']} -- subaccount {subaccount_id} buying power already below the tick's floor")
                skipped_untradeable += 1
                continue

            try:
                instrument = get_instrument(c["instrument"])
            except Exception as e:
                print(f"{c['instrument']} not tradeable on testnet, skipping: {e}")
                skipped_untradeable += 1
                continue

            try:
                ticker = get_ticker(c["instrument"])
            except Exception as e:
                print(f"{c['instrument']} ticker fetch failed, skipping: {e}")
                skipped_untradeable += 1
                continue

            mark_price = ticker.get("mark_price")
            index_price = ticker.get("index_price")
            if not mark_price or float(mark_price) <= 0 or not index_price:
                print(f"{c['instrument']} has no live testnet price yet, skipping")
                skipped_untradeable += 1
                continue

            amount = _size_for_fee_coverage(instrument, float(mark_price), float(index_price))
            if amount is None:
                print(f"{c['instrument']} can't clear {MIN_FEE_COVERAGE_MULTIPLE}x fee coverage even at {MAX_SIZE_MULTIPLE}x minimum size, skipping")
                skipped_uneconomical += 1
                continue

            try:
                utilization = _margin_utilization_after(current_states[subaccount_id], c["instrument"], c["side"], amount, c["asset"])
            except Exception as e:
                print(f"{c['instrument']} margin simulation failed, skipping: {e}")
                skipped_untradeable += 1
                continue
            if utilization > config["max_margin_utilization_fraction"]:
                print(f"{c['instrument']} skipped on {config['name']} -- would push margin utilization to {utilization:.1%}, over the {config['max_margin_utilization_fraction']:.0%} cap")
                skipped_margin_cap += 1
                continue

            try:
                order_result = place_order(
                    instrument_name=c["instrument"],
                    direction=c["side"],
                    amount=amount,
                    limit_price=mark_price,
                    subaccount_id=subaccount_id,
                )
            except Exception as e:
                print(f"order placement failed for {c['instrument']} on {config['name']}, skipping: {e}")
                skipped_untradeable += 1
                continue

            order = order_result["order"]
            entry_price = float(order["limit_price"])

            stop_order_id = None
            if c["side"] == "sell":
                stop_order_id = _attach_stop_loss(
                    instrument_name=c["instrument"],
                    subaccount_id=subaccount_id,
                    entry_price=entry_price,
                    amount=amount,
                    expiry_sec=int(c["expiry"]),
                    now_sec=int(now_ms / 1000),
                )

            state = get_account_state(subaccount_id)
            current_states[subaccount_id] = state
            margin = _margin_for_instrument(state, c["instrument"])

            current_buying_power = float(state.get("initial_margin") or 0)
            if current_buying_power < buying_power_floors[subaccount_id]:
                low_buying_power[subaccount_id] = True
                print(f"subaccount {subaccount_id} ({config['name']}) buying power ${current_buying_power:.2f} fell below the tick's floor ${buying_power_floors[subaccount_id]:.2f} -- no more real orders on it this tick")

            try:
                conn.execute(
                    """
                    INSERT INTO paper_trades
                        (source_wallet_address, source_event_id, instrument, asset, option_type, strike, expiry,
                         side, mode, intended_price, entry_price, entry_ts, fill_status, testnet_order_id,
                         margin_required_usd, edge_bucket_n, edge_bucket_win_rate, edge_bucket_wilson_low,
                         edge_bucket_median_return_pct, created_at, subaccount_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'testnet_order', ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (wallet, c["id"], c["instrument"], c["asset"], c["option_type"], c["strike"], c["expiry"],
                     c["side"], c["price"], entry_price, now_ms, order["order_id"],
                     margin,
                     edge["n"] if edge else None, edge["win_rate"] if edge else None,
                     edge["wilson_low"] if edge else None, edge["median_return_pct"] if edge else None,
                     now_ms, subaccount_id),
                )
                placed += 1
                stop_note = f", stop-loss {stop_order_id}" if stop_order_id else (", UNPROTECTED (no stop)" if c["side"] == "sell" else "")
                print(f"placed real order {order['order_id']} for {c['instrument']} ({c['side']}) on {config['name']} (subaccount {subaccount_id}), margin=${margin}{stop_note}")
            except INTEGRITY_ERRORS:
                pass

    conn.commit()
    return placed, skipped_weak_edge, skipped_wrong_asset, skipped_uneconomical, skipped_untradeable, skipped_margin_cap


def run():
    if not EXECUTION_ENABLED:
        print("EXECUTION_ENABLED is false -- refusing to place any real orders. Set it in .env to run this for real.")
        return

    conn = get_live_conn()
    conn.execute("PRAGMA busy_timeout = 8000")

    placed, skipped_weak_edge, skipped_wrong_asset, skipped_uneconomical, skipped_untradeable, skipped_margin_cap = run_live_tick(conn)

    print(
        f"placed {placed} real order(s), skipped {skipped_weak_edge} weak-edge, "
        f"skipped {skipped_wrong_asset} ineligible asset/side, skipped {skipped_uneconomical} uneconomical, "
        f"skipped {skipped_untradeable} untradeable-on-testnet, skipped {skipped_margin_cap} over the margin cap"
    )
    conn.close()


if __name__ == "__main__":
    run()
