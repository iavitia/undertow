"""Options-wheel backtest: copies the active watchlist's ETH sell-put /
sell-call signals into a two-stage wheel state machine, against a fixed
$300 account. See conversation for the full spec this implements.

Design decisions worth being explicit about (several weren't fully
pinned down in the original spec):

- Fees: estimate_fee()'s formula (base_fee + taker_fee_rate * notional)
  needs per-instrument fields from get_instrument(), but historical
  instruments (anything expired) don't exist on live testnet anymore --
  there's no way to query their real fee fields. Uses the CURRENT,
  confirmed-live schedule (base_fee=$0.50, taker_fee_rate=0.0003)
  applied uniformly across the whole backtest instead, since that's the
  best available proxy and happens to match the spec's own fallback
  assumption almost exactly. Fees only apply to the option entry trade
  itself -- the "buy/sell ETH at settlement" step on assignment/
  call-away is a simulation abstraction for cash-settlement mechanics
  (Derive has no tradeable spot ETH market, confirmed earlier this
  session), not a real trade, so it carries no separate fee.

- Settlement: a copied position is ALWAYS held to its own natural
  expiry and settled via intrinsic value there -- deliberately not
  mirroring the source wallet's own exit (which may have been early),
  since the wheel's own discipline ("if a put settles ITM...") is
  explicitly about expiry outcomes, not the source wallet's behavior
  after signaling. This means position construction here is custom,
  not reused wholesale from backtest_copy_trades.simulate_copy_trades
  (which does mirror the source wallet's close) -- though its lower-
  level pricing/spot helpers are reused directly.

- Cost basis after an unassigned call: the spec defines the INITIAL
  cost basis at put-assignment (strike - premium) but doesn't say
  whether it keeps declining as further calls are sold and expire
  worthless while still in Stage 2. Implemented as yes -- each
  unassigned call's premium further reduces cost basis, which is
  standard wheel bookkeeping and keeps the "strike >= cost_basis"
  discipline meaningful across multiple Stage-2 cycles rather than
  just the first one.

- Comparison (b), the delta-targeted rules wheel: there is no
  historical Greeks/IV data anywhere (confirmed multiple times this
  session -- Derive's ticker is live-only). 0.20-delta puts / 0.30-delta
  calls are approximated with fixed %-OTM bands instead (see
  RULES_PUT_OTM_PCT/RULES_CALL_OTM_PCT below) -- a labeled
  approximation, not real delta targeting.
"""
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import sqlite3

from config import DB_PATH
from scripts.backtest_copy_trades import (
    build_instrument_price_index,
    build_spot_price_index,
    nearest_spot_price,
    next_other_wallet_print,
)


def load_events_eth(conn):
    """ETH-only options_events, typed columns trusted directly -- see
    conversation and scripts/repair_options_events_shift.py:
    mark_price/rfq_id/tx_status/trade_fee/liquidity_role/realized_pnl/
    raw_json were shifted out of place for 985k rows (ingested before the
    V3 cutover); the repair migration fixed this in place on
    {2026-10-11}, so rfq_id (the only one of those this script needs, for
    next_other_wallet_print's own-RFQ exclusion) no longer needs the
    raw-JSON workaround an earlier version of this function had."""
    return pd.read_sql_query(
        """
        SELECT wallet_address, instrument, asset, option_type, strike, expiry, side, size, price,
               index_price_usd, timestamp, rfq_id, liquidity_role
        FROM options_events
        WHERE wallet_address IS NOT NULL AND asset = 'ETH'
        """,
        conn,
    )

SIZE = 0.1
RESERVE_USDC = 50.0
START_EQUITY = 300.0

FEE_BASE = 0.50
FEE_RATE = 0.0003


def estimate_fee_fixed(size, index_price):
    return FEE_BASE + FEE_RATE * size * index_price


# Rough %-out-of-the-money proxies for 0.20-delta puts / 0.30-delta
# calls on ~14-21 DTE crypto options (see module docstring -- no real
# historical delta data exists to target these precisely).
RULES_PUT_OTM_PCT = 0.10
RULES_CALL_OTM_PCT = 0.08
RULES_MIN_DTE_DAYS = 14
RULES_MAX_DTE_DAYS = 21

# Regime classification: rolling 30-day ETH spot return at each
# timestamp. Thresholds are a plain, stated choice, not derived from
# anything -- +/-15% over 30 days to count as a trending regime,
# otherwise flat.
REGIME_WINDOW_MS = 30 * 86400 * 1000
REGIME_TREND_PCT = 0.15


def build_signals(df, active_wallets):
    """Every (wallet, instrument) ETH sell-put/sell-call entry from the
    active watchlist, chronologically sorted by entry_ts. Unlike
    simulate_copy_trades, deliberately does NOT determine an exit here
    -- the wheel settles everything at natural expiry regardless of
    what the source wallet did (see module docstring). df is already
    ETH-only (load_events_eth)."""
    sells = df[df["wallet_address"].isin(active_wallets) & (df["side"] == "sell")]
    grouped = sells.sort_values("timestamp").groupby(["wallet_address", "instrument"]).agg(
        option_type=("option_type", "first"),
        strike=("strike", "first"),
        expiry=("expiry", "first"),
        entry_ts=("timestamp", "min"),
    ).reset_index()
    grouped = grouped[grouped["option_type"].isin(["call", "put"])]
    return grouped.sort_values("entry_ts").to_dict("records")


def entry_price_for(price_index, instrument, wallet, entry_ts, own_rfqs):
    # Excludes the signal wallet's own bilateral RFQ counterparty print --
    # same reason backtest_copy_trades.simulate_copy_trades always does
    # this: an RFQ's two legs land at an identical pre-agreed price
    # seconds apart, so without the exclusion "next other wallet print"
    # is very often just the guaranteed other half of the wallet's own
    # trade, not real price discovery (see conversation -- this is the
    # exact artifact that made one wallet's apparent edge turn out to be
    # a pricing tautology earlier this project).
    # Every wheel signal is a sell, so the copy price is the next taker-SELL
    # print (the bid) after COPY_LATENCY_MS -- see backtest_copy_trades'
    # module docstring for why this also excludes the counterparty leg of
    # the signal wallet's own orderbook fill.
    excluded = own_rfqs.get((wallet, instrument), set())
    price, _ts, _index = next_other_wallet_print(price_index, instrument, wallet, entry_ts, excluded, "sell")
    return price


def settle_intrinsic(spot_index, asset, strike, option_type, expiry_ms):
    spot = nearest_spot_price(spot_index, asset, expiry_ms)
    if spot is None:
        return None, None
    intrinsic = max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)
    return intrinsic, spot


def classify_regime(spot_index, asset, ts_ms):
    now_spot = nearest_spot_price(spot_index, asset, ts_ms)
    past_spot = nearest_spot_price(spot_index, asset, ts_ms - REGIME_WINDOW_MS)
    if now_spot is None or past_spot is None or past_spot <= 0:
        return "unknown"
    change = (now_spot - past_spot) / past_spot
    if change >= REGIME_TREND_PCT:
        return "rising"
    if change <= -REGIME_TREND_PCT:
        return "falling"
    return "flat"


def run_copy_wheel(signals, price_index, spot_index, own_rfqs, start_cash, start_eth):
    """The signal-driven wheel described in the spec. Returns a result
    dict with equity curve, trade log, skip counts, and stage-time
    tracking."""
    cash = start_cash
    eth_held = start_eth
    stage = 2 if start_eth > 0 else 1
    # Starting already in stage 2 (eth-portfolio's real setup) never goes
    # through a put assignment to set cost_basis the normal way -- use
    # ETH's spot at the start of the backtest window as the basis for
    # already-held ETH, same convention as "what you paid for it."
    cost_basis = nearest_spot_price(spot_index, "ETH", signals[0]["entry_ts"]) if start_eth > 0 and signals else None
    open_pos = None  # dict: option_type, strike, expiry_ms, entry_price, entry_ts, instrument

    equity_curve = []  # (ts_ms, equity_usd)
    trades = []  # dicts, for the report
    skipped = {"position_open": 0, "insufficient_reserve": 0, "below_cost_basis": 0, "wrong_stage": 0, "no_copy_price": 0}
    stage_time_ms = {1: 0, 2: 0}
    last_ts = signals[0]["entry_ts"] if signals else int(time.time() * 1000)
    puts_assigned = 0
    calls_assigned = 0
    total_premium = 0.0
    total_fees = 0.0
    # Separately tracked so final P&L can be split into "option leg" vs
    # "ETH holding" (see conversation -- requested specifically to audit
    # the equity accounting, not just report a final number).
    # total_intrinsic_settlements: cumulative cash-settled option payoff
    # owed at ITM expiry (always <= 0, we're always the seller paying it).
    # total_eth_trade_cash: cumulative cash spent acquiring ETH (negative,
    # on put assignment) or received disposing of it (positive, on being
    # called away) -- NOT the same as option premium/fees, and NOT the
    # same as the unrealized mark-to-market gain/loss on ETH still held.
    total_intrinsic_settlements = 0.0
    total_eth_trade_cash = 0.0

    def mark_equity(ts_ms, spot_hint=None):
        spot = spot_hint if spot_hint is not None else nearest_spot_price(spot_index, "ETH", ts_ms)
        eth_value = eth_held * spot if spot is not None else 0.0
        equity_curve.append((ts_ms, cash + eth_value))

    def resolve_open_position_if_due(now_ts):
        nonlocal open_pos, cash, eth_held, stage, cost_basis, puts_assigned, calls_assigned
        nonlocal total_intrinsic_settlements, total_eth_trade_cash
        if open_pos is None or now_ts < open_pos["expiry_ms"]:
            return
        intrinsic, spot = settle_intrinsic(spot_index, "ETH", open_pos["strike"], open_pos["option_type"], open_pos["expiry_ms"])
        if intrinsic is None:
            # no spot data at expiry -- can't settle, drop the position rather than hang forever
            open_pos = None
            return

        cash -= intrinsic * SIZE  # cash-settled option payoff owed
        total_intrinsic_settlements -= intrinsic * SIZE
        if open_pos["option_type"] == "put":
            if intrinsic > 0:  # ITM -- assigned
                cash -= spot * SIZE  # acquire the ETH at settlement price
                total_eth_trade_cash -= spot * SIZE
                eth_held = SIZE
                cost_basis = open_pos["strike"] - open_pos["entry_price"]
                stage = 2
                puts_assigned += 1
                trades[-1]["outcome"] = "assigned"
            else:
                trades[-1]["outcome"] = "expired_worthless"
        else:  # call
            if intrinsic > 0:  # ITM -- called away
                cash += spot * SIZE  # sell the ETH at settlement price
                total_eth_trade_cash += spot * SIZE
                eth_held = 0.0
                cost_basis = None
                stage = 1
                calls_assigned += 1
                trades[-1]["outcome"] = "called_away"
            else:
                cost_basis -= open_pos["entry_price"]  # unassigned call premium further lowers cost basis
                trades[-1]["outcome"] = "expired_worthless"
        trades[-1]["settled_spot"] = spot
        trades[-1]["intrinsic"] = intrinsic
        open_pos = None

    for sig in signals:
        ts = sig["entry_ts"]
        resolve_open_position_if_due(ts)
        stage_time_ms[stage] += max(ts - last_ts, 0)
        last_ts = ts

        if open_pos is not None:
            skipped["position_open"] += 1
            continue
        if stage == 1 and sig["option_type"] != "put":
            skipped["wrong_stage"] += 1
            continue
        if stage == 2 and sig["option_type"] != "call":
            skipped["wrong_stage"] += 1
            continue

        if stage == 1:
            if (cash - RESERVE_USDC) < sig["strike"] * SIZE:
                skipped["insufficient_reserve"] += 1
                continue
            price = entry_price_for(price_index, sig["instrument"], sig["wallet_address"], ts, own_rfqs)
            if price is None or price <= 0:
                skipped["no_copy_price"] += 1
                continue
        else:
            if sig["strike"] < cost_basis:
                skipped["below_cost_basis"] += 1
                continue
            price = entry_price_for(price_index, sig["instrument"], sig["wallet_address"], ts, own_rfqs)
            if price is None or price <= 0:
                skipped["no_copy_price"] += 1
                continue

        index_price_at_entry = nearest_spot_price(spot_index, "ETH", ts) or sig["strike"]
        fee = estimate_fee_fixed(SIZE, index_price_at_entry)
        premium = price * SIZE
        cash += premium - fee
        total_premium += premium
        total_fees += fee

        open_pos = {
            "option_type": sig["option_type"], "strike": sig["strike"],
            "expiry_ms": int(sig["expiry"] * 1000), "entry_price": price, "entry_ts": ts,
            "instrument": sig["instrument"],
        }
        trades.append({
            "entry_ts": ts, "option_type": sig["option_type"], "strike": sig["strike"],
            "expiry_ms": open_pos["expiry_ms"], "entry_price": price, "premium": premium, "fee": fee,
            "stage_entered": stage,
        })
        mark_equity(ts)

    # settle anything still open at its own expiry, using the last signal time as a cursor
    if open_pos is not None:
        final_pos_expiry = open_pos["expiry_ms"]
        resolve_open_position_if_due(final_pos_expiry)
        last_ts = final_pos_expiry
        mark_equity(final_pos_expiry)

    final_spot = nearest_spot_price(spot_index, "ETH", last_ts)
    final_equity = cash + eth_held * (final_spot or 0)

    # P&L split, exact by construction (see the tracked cash-flow totals
    # above) rather than inferred from start/end alone -- option_pnl is
    # premiums collected minus fees minus any ITM settlement payouts
    # (independent of whether ETH was subsequently bought/sold);
    # eth_holding_pnl is everything from holding/trading the ETH itself:
    # net cash spent/received acquiring or disposing of it, plus the
    # unrealized mark-to-market change on whatever's still held at the
    # end versus what was held (if any) at the start. The two should sum
    # to exactly final_equity - start_equity.
    option_pnl = total_premium - total_fees + total_intrinsic_settlements
    start_equity = start_cash + start_eth * (nearest_spot_price(spot_index, "ETH", signals[0]["entry_ts"]) if signals else 0)
    eth_holding_pnl = total_eth_trade_cash + eth_held * (final_spot or 0) - start_eth * (nearest_spot_price(spot_index, "ETH", signals[0]["entry_ts"]) if signals else 0)

    return {
        "cash": cash, "eth_held": eth_held, "final_equity": final_equity, "final_spot": final_spot,
        "last_ts": last_ts, "start_equity": start_equity,
        "equity_curve": equity_curve, "trades": trades, "skipped": skipped,
        "stage_time_ms": stage_time_ms, "puts_assigned": puts_assigned, "calls_assigned": calls_assigned,
        "total_premium": total_premium, "total_fees": total_fees,
        "option_pnl": option_pnl, "eth_holding_pnl": eth_holding_pnl,
    }


def max_drawdown_usd(equity_curve):
    if not equity_curve:
        return 0.0
    peak = equity_curve[0][1]
    worst = 0.0
    for _ts, eq in equity_curve:
        peak = max(peak, eq)
        worst = min(worst, eq - peak)
    return worst


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    active = {r["wallet_address"] for r in conn.execute("SELECT wallet_address FROM watchlist WHERE is_active = 1").fetchall()}
    print(f"{len(active)} active watchlist wallets")

    df = load_events_eth(conn)
    conn.close()
    price_index = build_instrument_price_index(df)
    spot_index = build_spot_price_index(df)
    own_rfqs = df.groupby(["wallet_address", "instrument"])["rfq_id"].apply(lambda s: set(s.dropna())).to_dict()

    signals = build_signals(df, active)
    puts = sum(1 for s in signals if s["option_type"] == "put")
    calls = sum(1 for s in signals if s["option_type"] == "call")
    print(f"{len(signals)} ETH sell signals from active wallets ({puts} put, {calls} call)")
    print()

    start_spot = nearest_spot_price(spot_index, "ETH", signals[0]["entry_ts"]) if signals else None
    print(f"ETH spot at start of backtest window: ${start_spot:.2f}" if start_spot else "no start spot found")

    print("=== Account A: starts 0.1 ETH + $50 USDC (matches real eth-portfolio setup) ===")
    result_a = run_copy_wheel(signals, price_index, spot_index, own_rfqs, start_cash=RESERVE_USDC, start_eth=SIZE)
    print(f"  (real starting equity: ${result_a['start_equity']:.2f} -- 0.1 ETH @ ${start_spot:.2f} + $50, NOT a flat $300)")
    report(result_a, show_pnl_split=True)

    print()
    print("=== Account B: starts $300 all-USDC ===")
    result_b = run_copy_wheel(signals, price_index, spot_index, own_rfqs, start_cash=START_EQUITY, start_eth=0.0)
    report(result_b, show_pnl_split=True)

    print()
    print("=== Comparison (a): buy-and-hold 0.1 ETH + $50 USDC, no trading ===")
    end_spot = result_a["final_spot"]
    print(f"  ETH spot: ${start_spot:.2f} -> ${end_spot:.2f}  ({(end_spot/start_spot - 1)*100:+.1f}%)")
    hold_final = SIZE * end_spot + RESERVE_USDC
    hold_start = SIZE * start_spot + RESERVE_USDC
    print(f"  hold equity: ${hold_start:.2f} -> ${hold_final:.2f}  (return {(hold_final/hold_start - 1)*100:+.1f}%)")
    print(f"  vs. Account A's wheel: ${result_a['final_equity']:.2f}  (return {(result_a['final_equity']/result_a['start_equity'] - 1)*100:+.1f}%)")
    print(f"  wheel - hold = ${result_a['final_equity'] - hold_final:+.2f}")

    return result_a, result_b, signals, price_index, spot_index, start_spot


def report(result, start_equity=None, show_pnl_split=False):
    # Uses the account's own real starting equity (tracked inside
    # run_copy_wheel), not a flat assumed figure -- Account A starts
    # already holding 0.1 ETH, so its true starting value is $50 +
    # 0.1*start_spot, NOT a flat $300. Confirmed live this session: using
    # a flat $300 here understated Account A's real loss as a fake gain
    # (+23.1% vs the true -13.4%) -- exactly the kind of error the P&L
    # split's "should match exactly" check exists to catch.
    start_equity = result["start_equity"] if start_equity is None else start_equity
    final = result["final_equity"]
    ret = (final / start_equity - 1) * 100
    dd = max_drawdown_usd(result["equity_curve"])
    n_trades = len(result["trades"])
    total_skipped = sum(result["skipped"].values())
    fee_pct = (result["total_fees"] / result["total_premium"] * 100) if result["total_premium"] else 0
    stage1_days = result["stage_time_ms"][1] / 86400000
    stage2_days = result["stage_time_ms"][2] / 86400000

    print(f"  ending equity: ${final:.2f}  (return {ret:+.1f}%)")
    print(f"  max drawdown: ${dd:.2f}")
    print(f"  trades taken: {n_trades}  (puts assigned: {result['puts_assigned']}, calls assigned/called away: {result['calls_assigned']})")
    print(f"  trades skipped: {total_skipped}  {result['skipped']}")
    print(f"  total premium collected: ${result['total_premium']:.2f}  total fees: ${result['total_fees']:.2f}  (fees = {fee_pct:.2f}% of premium)")
    print(f"  time in stage 1 (cash/put-selling): {stage1_days:.0f} days  |  stage 2 (ETH held/call-selling): {stage2_days:.0f} days")
    if show_pnl_split:
        split_sum = result["option_pnl"] + result["eth_holding_pnl"]
        actual_pnl = result["final_equity"] - result["start_equity"]
        print(f"  P&L split: option premium/fees/settlements = ${result['option_pnl']:+.2f}  |  ETH holding (price + trade cash flows) = ${result['eth_holding_pnl']:+.2f}")
        print(f"  split sum = ${split_sum:+.2f}  vs. actual equity change = ${actual_pnl:+.2f}  (should match exactly: {'OK' if abs(split_sum - actual_pnl) < 0.01 else 'MISMATCH'})")


if __name__ == "__main__":
    run()
