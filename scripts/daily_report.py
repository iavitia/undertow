"""Daily report: how much activity actually happened, paper vs. real.

Two genuinely different numbers, not duplicates of each other:
  - paper_trades (mode='simulated'/'testnet_order') is OUR OWN record of
    what the agent attempted -- a candidate it decided to copy, and (for
    testnet_order rows) tried to place a real order for.
  - Derive's own /private/get_trade_history is ground truth for what
    actually, confirmably landed on the exchange -- independent of
    whether our attempt succeeded. See conversation: the stale-scheduled-
    run bug meant several real order attempts crashed before ever
    reaching Derive, so "we have a testnet_order row" does NOT by itself
    mean a real fill happened. This report checks both and shows where
    they diverge, instead of trusting our own DB as if it were the
    exchange."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.derive_execution_client import get_open_orders, get_trade_history
from config import (
    DERIVE_SUBACCOUNT_ID,
    DERIVE_SUBACCOUNT_ID_ETH_PORTFOLIO,
    DERIVE_SUBACCOUNT_ID_FAST,
    DERIVE_SUBACCOUNT_ID_SOL,
)
from db.cloud_conn import get_live_conn

WINDOW_HOURS = 24

SUBACCOUNTS = [
    ("prime", DERIVE_SUBACCOUNT_ID),
    ("alt", DERIVE_SUBACCOUNT_ID_SOL),
    ("fast", DERIVE_SUBACCOUNT_ID_FAST),
]


def paper_trading_summary(conn, since_ms, now_ms):
    opened = conn.execute(
        "SELECT mode, fill_status FROM paper_trades WHERE created_at >= ? AND created_at < ?",
        (since_ms, now_ms),
    ).fetchall()
    resolved = conn.execute(
        """
        SELECT mode, fill_status, return_pct FROM paper_trades
        WHERE resolved_ts IS NOT NULL AND resolved_ts >= ? AND resolved_ts < ?
        """,
        (since_ms, now_ms),
    ).fetchall()

    by_mode_opened = {}
    for r in opened:
        by_mode_opened.setdefault(r["mode"], {}).setdefault(r["fill_status"], 0)
        by_mode_opened[r["mode"]][r["fill_status"]] += 1

    by_mode_resolved = {}
    for r in resolved:
        by_mode_resolved.setdefault(r["mode"], []).append(r["return_pct"])

    return by_mode_opened, by_mode_resolved


def real_exchange_summary(since_ms, now_ms):
    """Ground truth: queries Derive directly for each real subaccount, not
    our DB. Returns {name: {"fills": [...], "resting": [...]}} -- fills
    (executed trades in the window) and resting (orders currently still
    on the book, regardless of window) are genuinely different states;
    collapsing them would hide the case this report exists to catch: an
    order our DB marked 'open' that is neither."""
    out = {}
    for name, subaccount_id in SUBACCOUNTS:
        if subaccount_id is None:
            continue
        fills = get_trade_history(subaccount_id, since_ms, now_ms, page_size=200).get("trades", [])
        resting = get_open_orders(subaccount_id).get("orders", [])
        out[name] = {"fills": fills, "resting": resting}
    return out


def most_recent_activity(conn, now_ms):
    """Time since the last paper_trades row of any kind, regardless of the
    report window -- self-diagnosing when the window itself shows nothing,
    which is exactly the case this report exists to catch (see
    conversation: GitHub Actions' schedule trigger has been firing far
    less often than configured)."""
    r = conn.execute("SELECT MAX(created_at) AS mx FROM paper_trades").fetchone()
    if r["mx"] is None:
        return None
    return (now_ms - r["mx"]) / 3600000


WATCHLIST_SILENCE_ALERT_HOURS = 48


def watchlist_feed_health(conn, since_ms, now_ms):
    """(trades ingested from active watchlist wallets in the window, hours
    since the last one, distinct wallets of any kind trading in the
    window). The first number going to zero while the third stays high is
    the signature of the 2026-10-07 incident: Derive's V3 API reported a
    different `wallet` address for every trader, so 42 wallets that had
    been trading daily all appeared to stop at once while hundreds of
    others kept going -- the pipeline ran clean and copied nothing for 10
    days. Counts only, never addresses."""
    r = conn.execute(
        """
        SELECT COUNT(*) FILTER (WHERE oe.timestamp >= ? AND oe.timestamp < ?) AS n_window,
               MAX(oe.timestamp) AS last_ts
        FROM options_events oe JOIN watchlist w ON w.wallet_address = oe.wallet_address
        WHERE w.is_active = 1
        """,
        (since_ms, now_ms),
    ).fetchone()
    others = conn.execute(
        "SELECT COUNT(DISTINCT wallet_address) AS n FROM options_events WHERE timestamp >= ? AND timestamp < ?",
        (since_ms, now_ms),
    ).fetchone()
    hours_since = (now_ms - r["last_ts"]) / 3600000 if r["last_ts"] is not None else None
    return r["n_window"], hours_since, others["n"]


def eth_portfolio_vs_mainnet(conn):
    """eth-portfolio's testnet result beside its mainnet-shadow estimate,
    all time (see run_live_agent._mainnet_shadow_entry). Testnet returns
    are gross and only exist for orders testnet actually filled; shadow
    returns are net of mainnet fees and exist for every order mainnet
    could have filled as a taker, including ones testnet never filled.
    The like-for-like gap is computed only on rows where both resolved."""
    if DERIVE_SUBACCOUNT_ID_ETH_PORTFOLIO is None:
        return None
    return conn.execute(
        """
        SELECT COUNT(*) AS orders,
               COUNT(*) FILTER (WHERE fill_status = 'never_filled') AS never_filled,
               COUNT(*) FILTER (WHERE shadow_entry_price IS NOT NULL) AS shadow_quoted,
               COUNT(*) FILTER (WHERE fill_status IN ('resolved', 'stopped_out')) AS tn_n,
               AVG(return_pct) FILTER (WHERE fill_status IN ('resolved', 'stopped_out')) AS tn_avg,
               COUNT(*) FILTER (WHERE fill_status IN ('resolved', 'stopped_out') AND return_pct > 0) AS tn_wins,
               COUNT(*) FILTER (WHERE shadow_return_pct IS NOT NULL) AS sh_n,
               AVG(shadow_return_pct) AS sh_avg,
               COUNT(*) FILTER (WHERE shadow_return_pct > 0) AS sh_wins,
               COUNT(*) FILTER (WHERE fill_status IN ('resolved', 'stopped_out') AND shadow_return_pct IS NOT NULL) AS both_n,
               AVG(return_pct - shadow_return_pct) FILTER (WHERE fill_status IN ('resolved', 'stopped_out') AND shadow_return_pct IS NOT NULL) AS gap_avg
        FROM paper_trades
        WHERE mode = 'testnet_order' AND subaccount_id = ? AND fill_status <> 'skipped_weak_edge'
        """,
        (DERIVE_SUBACCOUNT_ID_ETH_PORTFOLIO,),
    ).fetchone()


BACKTEST_SPREAD = 0.12              # what the rule backtest assumed a sale gives up vs mark
BACKTEST_PER_TRADE_ON_MARGIN = 0.0044  # backtest at 0.05 BTC, 2024-06 -> 2026-06, full mainnet costs


def rule_paper_summary(conn):
    """Each rule's mainnet paper run (scripts/rule_paper.py, all time) vs
    the backtest's assumptions. P&L is also recomputed at 0.02 BTC -- the
    fee is mostly flat, so smaller sizes keep less of the premium."""
    out = []
    for name in [r["rule_name"] for r in conn.execute("SELECT DISTINCT rule_name FROM rule_paper_trades ORDER BY 1").fetchall()]:
        rows = conn.execute("SELECT * FROM rule_paper_trades WHERE rule_name = ?", (name,)).fetchall()
        entered = [r for r in rows if r["status"] != "skipped"]
        done = [r for r in rows if r["status"] == "resolved"]
        skips = {}
        for r in rows:
            if r["status"] == "skipped":
                skips[r["skip_reason"]] = skips.get(r["skip_reason"], 0) + 1
        spreads = [1 - r["bid"] / r["mark"] for r in entered if r["mark"]]
        deep = [r for r in entered if r["bid_amount"] is not None and r["bid_amount"] >= r["size"]]

        def pnl_at(r, size):
            fee = 0.5 + min(0.0003 * size * r["spot_at_entry"], 0.125 * r["bid"] * size)
            return (r["bid"] - r["exit_value"]) * size - fee

        margins = [r["margin_usd"] for r in done if r["margin_usd"]]
        out.append({
            "name": name, "days": len(rows), "entered": len(entered), "open": len(entered) - len(done), "skips": skips,
            "resolved": len(done), "wins": sum(r["pnl_usd"] > 0 for r in done),
            "pnl": sum(r["pnl_usd"] for r in done), "pnl_002": sum(pnl_at(r, 0.02) for r in done),
            "per_trade_on_margin": (sum(r["pnl_usd"] for r in done) / len(done)) / (sum(margins) / len(margins)) if margins and done else None,
            "median_spread": sorted(spreads)[len(spreads) // 2] if spreads else None,
            "deep_share": len(deep) / len(entered) if entered else None,
            "size": entered[0]["size"] if entered else None,
        })
    return out


def run(window_hours=WINDOW_HOURS):
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - window_hours * 3600 * 1000

    conn = get_live_conn()
    by_mode_opened, by_mode_resolved = paper_trading_summary(conn, since_ms, now_ms)
    last_activity_h = most_recent_activity(conn, now_ms)
    wl_trades, wl_silent_h, market_wallets = watchlist_feed_health(conn, since_ms, now_ms)
    eth_pf = eth_portfolio_vs_mainnet(conn)
    rules = rule_paper_summary(conn)
    conn.close()

    print(f"=== Daily report -- trailing {window_hours}h ===\n")
    if last_activity_h is None:
        print("No paper_trades rows exist at all.\n")
    elif last_activity_h > window_hours:
        print(f"NOTE: nothing in this {window_hours}h window, but the most recent activity of ANY kind was {last_activity_h:.1f}h ago -- the pipeline has been idle longer than this window, not just quiet today.\n")

    print("-- Watchlist feed --")
    silent_str = f"{wl_silent_h:.1f}h ago" if wl_silent_h is not None else "never"
    print(f"  {wl_trades} trade(s) from active watchlist wallets in window (last one {silent_str}); {market_wallets} distinct wallets traded market-wide")
    if wl_silent_h is None or wl_silent_h > WATCHLIST_SILENCE_ALERT_HOURS:
        print(f"  WARNING: no watchlist trade in over {WATCHLIST_SILENCE_ALERT_HOURS}h while the market kept trading -- check that Derive's `wallet` field still matches watchlist addresses before assuming the wallets went quiet")
    print()

    print("-- Paper-trading record (our own DB) --")
    if not by_mode_opened:
        print("  nothing opened in this window")
    for mode, statuses in by_mode_opened.items():
        total = sum(statuses.values())
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
        print(f"  {mode}: {total} opened ({breakdown})")

    for mode, returns in by_mode_resolved.items():
        n = len(returns)
        wins = sum(1 for r in returns if r is not None and r > 0)
        avg_ret = (sum(r for r in returns if r is not None) / n * 100) if n else None
        avg_str = f"{avg_ret:.1f}%" if avg_ret is not None else "n/a"
        print(f"  {mode}: {n} resolved, {wins}/{n} wins, avg return/trade={avg_str}")

    if eth_pf is not None:
        pct = lambda v: f"{v * 100:.1f}%" if v is not None else "n/a"
        print("\n-- eth-portfolio: testnet vs mainnet estimate (all time) --")
        print(f"  {eth_pf['orders']} order(s): {eth_pf['never_filled']} never filled on testnet, {eth_pf['shadow_quoted']} had a mainnet quote to fill against")
        print(f"  testnet actual (gross):        {eth_pf['tn_n']} resolved or stopped out, {eth_pf['tn_wins']} wins, avg return/trade {pct(eth_pf['tn_avg'])}")
        print(f"  mainnet estimate (net of fees): {eth_pf['sh_n']} resolved or stopped out, {eth_pf['sh_wins']} wins, avg return/trade {pct(eth_pf['sh_avg'])}")
        if eth_pf["both_n"]:
            print(f"  same {eth_pf['both_n']} trade(s) resolved on both: testnet beats the mainnet estimate by {pct(eth_pf['gap_avg'])} per trade on average")

    for r in rules:
        pct = lambda v: f"{v * 100:.1f}%" if v is not None else "n/a"
        print(f"\n-- Rule paper run on mainnet: {r['name']} (all time, no orders) --")
        print(f"  {r['days']} day(s): {r['entered']} entered ({r['open']} still open), skipped {r['skips'] or 'none'}")
        print(f"  settled: {r['resolved']}, {r['wins']} wins | P&L at {r['size']:g} BTC ${r['pnl']:+,.2f} (at 0.02 BTC ${r['pnl_002']:+,.2f})")
        print(f"  per trade on margin: {pct(r['per_trade_on_margin'])} (backtest expected {pct(BACKTEST_PER_TRADE_ON_MARGIN)})")
        print(f"  real spread given up at entry (1 - bid/mark): median {pct(r['median_spread'])} (backtest assumed {pct(BACKTEST_SPREAD)}) | bid deep enough for the full size: {pct(r['deep_share'])}")

    print("\n-- Real exchange record (Derive's own data, ground truth) --")
    real = real_exchange_summary(since_ms, now_ms)
    total_fills = 0
    total_resting = 0
    for name, data in real.items():
        fills, resting = data["fills"], data["resting"]
        print(f"  {name}: {len(fills)} fill(s) in-window, {len(resting)} order(s) currently resting")
        for t in fills:
            print(f"    FILL    {t.get('instrument_name')} {t.get('direction')} {t.get('trade_amount')} @ {t.get('trade_price')}")
        for o in resting:
            print(f"    RESTING {o.get('instrument_name')} {o.get('direction')} {o.get('amount')} filled={o.get('filled_amount')}")
        total_fills += len(fills)
        total_resting += len(resting)
    if not real:
        print("  no configured subaccounts to check")

    print(f"\n-- Comparison --")
    db_open = by_mode_opened.get("testnet_order", {}).get("open", 0)
    print(f"  rows our DB currently marks 'open' (real):  {db_open}")
    print(f"  real FILLS confirmed on Derive:             {total_fills}")
    print(f"  real orders still RESTING on Derive's book: {total_resting}")
    accounted_for = total_fills + total_resting
    if db_open > accounted_for:
        print(f"  -> {db_open - accounted_for} row(s) our DB thinks are open real positions are neither filled nor resting on the actual exchange -- phantom, not real exposure, and our own reconciliation never catches this (see conversation: resolve_paper_trades.py only ever checks an instrument's natural expiry, never whether a 'testnet_order' row's underlying order actually filled)")


if __name__ == "__main__":
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else WINDOW_HOURS
    run(hours)
