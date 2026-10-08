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
from decimal import Decimal
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
from config import DERIVE_SUBACCOUNT_ID, DERIVE_SUBACCOUNT_ID_SOL, EXECUTION_ENABLED
from db.cloud_conn import INTEGRITY_ERRORS, get_live_conn
from scripts.run_watchlist_agent import find_new_candidates

# All three assets, both sides, are eligible for real execution -- BTC/ETH
# naked-short margin ($100-300+/contract against $5-25 premium, confirmed
# via real testnet orders) used to rule BTC/ETH sells out entirely, but
# that was a blunt per-asset carve-out standing in for the real concern:
# margin eating too much of the account. MAX_MARGIN_UTILIZATION_FRACTION
# below replaces it with the actual constraint (see conversation) --
# every order, any asset/side, is pre-checked against it, so BTC/ETH
# sells are allowed through but capped by cost rather than excluded by
# asset.
LIVE_ASSETS = ("SOL", "BTC", "ETH")

# V3's risk universes split BTC/ETH (Prime) from SOL (Alt) -- they can't
# share a subaccount (see conversation), so each asset routes to its own,
# financially-independent subaccount/margin pool. Every per-subaccount
# check below (margin cap, buying-power floor) runs separately per
# subaccount_id, not pooled across both.
SUBACCOUNT_FOR_ASSET = {
    "BTC": DERIVE_SUBACCOUNT_ID,
    "ETH": DERIVE_SUBACCOUNT_ID,
    "SOL": DERIVE_SUBACCOUNT_ID_SOL,
}

# Standing portfolio-level cap, not a per-tick one: total margin currently
# committed across every real open position must never exceed this
# fraction of total account value (collaterals_initial_margin), checked
# fresh before every single order via a margin simulation (see
# _margin_utilization_after and conversation -- placing real orders to
# probe margin risks them actually filling on testnet's thin, one-sided
# books, which happened and left stray positions needing manual cleanup;
# simulate_margin() never touches the real order book). 0.5 at the
# account's current ~$100k value means ~$50k of margin in use, max.
MAX_MARGIN_UTILIZATION_FRACTION = 0.5

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


def _size_for_fee_coverage(instrument, limit_price, index_price):
    """Finds the smallest order size (a multiple of the instrument's own
    minimum_amount, respecting amount_step) where premium collected
    clears MIN_FEE_COVERAGE_MULTIPLE times the estimated fee -- see
    conversation and clients/derive_execution_client.estimate_fee.
    Returns None if even MAX_SIZE_MULTIPLE times the minimum can't clear
    it (an uneconomical instrument at any reasonable size, not just at
    the minimum)."""
    amount_step = Decimal(instrument["amount_step"])
    minimum = Decimal(instrument["minimum_amount"])
    multiple = Decimal(1)
    while multiple <= MAX_SIZE_MULTIPLE:
        amount = (minimum * multiple).quantize(amount_step)
        if amount < minimum:
            amount = minimum
        premium = float(amount) * limit_price
        fee = estimate_fee(instrument, amount, index_price)
        if premium >= MIN_FEE_COVERAGE_MULTIPLE * fee:
            return amount
        multiple *= 2
    return None


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
    fresh before every order (not just once per tick) against
    MAX_MARGIN_UTILIZATION_FRACTION -- the standing portfolio-level cap
    that replaced the old SOL-only/BTC-ETH-buy-only asset carve-out (see
    conversation).

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
    entry, tries to place a real testnet order instead of recording a
    simulated fill. All three assets, both sides (see LIVE_ASSETS), routed
    to the right subaccount per asset (see SUBACCOUNT_FOR_ASSET -- BTC/ETH
    and SOL are financially separate subaccounts under V3), sized to clear
    a real fee multiple rather than always trading at instrument minimum
    (see _size_for_fee_coverage), checked against the standing portfolio
    margin cap before every order (see
    _margin_utilization_after/MAX_MARGIN_UTILIZATION_FRACTION, evaluated
    per subaccount), and stops placing new orders on a given subaccount
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
    # pooled) since BTC/ETH and SOL now sit in separate, independently
    # margined subaccounts -- see SUBACCOUNT_FOR_ASSET.
    subaccount_ids = set(SUBACCOUNT_FOR_ASSET.values())
    current_states = {sid: get_account_state(sid) for sid in subaccount_ids}
    buying_power_floors = {
        sid: float(state.get("initial_margin") or 0) * MIN_BUYING_POWER_FRACTION
        for sid, state in current_states.items()
    }
    low_buying_power = {sid: False for sid in subaccount_ids}

    for wallet, c, status, edge in find_new_candidates(conn, cursor_column="live_last_checked_ts"):
        if c["asset"] not in LIVE_ASSETS:
            skipped_wrong_asset += 1
            continue
        subaccount_id = SUBACCOUNT_FOR_ASSET[c["asset"]]

        if status == "skipped_weak_edge":
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

        # status == "pending_entry": try to mirror it for real.
        if low_buying_power[subaccount_id]:
            print(f"{c['instrument']} skipped -- subaccount {subaccount_id} buying power already below the tick's floor")
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
        if utilization > MAX_MARGIN_UTILIZATION_FRACTION:
            print(f"{c['instrument']} skipped -- would push margin utilization to {utilization:.1%}, over the {MAX_MARGIN_UTILIZATION_FRACTION:.0%} cap")
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
            print(f"order placement failed for {c['instrument']}, skipping: {e}")
            skipped_untradeable += 1
            continue

        order = order_result["order"]
        state = get_account_state(subaccount_id)
        current_states[subaccount_id] = state
        margin = _margin_for_instrument(state, c["instrument"])

        current_buying_power = float(state.get("initial_margin") or 0)
        if current_buying_power < buying_power_floors[subaccount_id]:
            low_buying_power[subaccount_id] = True
            print(f"subaccount {subaccount_id} buying power ${current_buying_power:.2f} fell below the tick's floor ${buying_power_floors[subaccount_id]:.2f} -- no more real orders on it this tick")

        try:
            conn.execute(
                """
                INSERT INTO paper_trades
                    (source_wallet_address, source_event_id, instrument, asset, option_type, strike, expiry,
                     side, mode, intended_price, entry_price, entry_ts, fill_status, testnet_order_id,
                     margin_required_usd, edge_bucket_n, edge_bucket_win_rate, edge_bucket_wilson_low,
                     edge_bucket_median_return_pct, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'testnet_order', ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)
                """,
                (wallet, c["id"], c["instrument"], c["asset"], c["option_type"], c["strike"], c["expiry"],
                 c["side"], c["price"], float(order["limit_price"]), now_ms, order["order_id"],
                 margin,
                 edge["n"] if edge else None, edge["win_rate"] if edge else None,
                 edge["wilson_low"] if edge else None, edge["median_return_pct"] if edge else None,
                 now_ms),
            )
            placed += 1
            print(f"placed real order {order['order_id']} for {c['instrument']} ({c['side']}), margin=${margin}")
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
