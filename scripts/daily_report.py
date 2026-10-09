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
from config import DERIVE_SUBACCOUNT_ID, DERIVE_SUBACCOUNT_ID_FAST, DERIVE_SUBACCOUNT_ID_SOL
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


def run(window_hours=WINDOW_HOURS):
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - window_hours * 3600 * 1000

    conn = get_live_conn()
    by_mode_opened, by_mode_resolved = paper_trading_summary(conn, since_ms, now_ms)
    last_activity_h = most_recent_activity(conn, now_ms)
    conn.close()

    print(f"=== Daily report -- trailing {window_hours}h ===\n")
    if last_activity_h is None:
        print("No paper_trades rows exist at all.\n")
    elif last_activity_h > window_hours:
        print(f"NOTE: nothing in this {window_hours}h window, but the most recent activity of ANY kind was {last_activity_h:.1f}h ago -- the pipeline has been idle longer than this window, not just quiet today.\n")

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
