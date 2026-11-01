"""Mainnet paper run for a rule-based strategy: what the rule would trade on
MAINNET each day, at real prices, with no orders placed.

Testnet can't answer this: its thin book almost never fills (0 of 31
entries, 2026-10-18 to 10-20), it lists far fewer short-dated strikes, and
it charges no base fee. Mainnet's public API gives the real bid, ask, mark,
depth, listed fee and required margin, so every entry here is a taker sale
at mainnet's best bid. That's the price a real order would get at that
moment, provided the bid's size covers the order (bid_amount is recorded
to check that).

The rule (RULES below): once a day at ENTRY_HOUR_UTC, sell the put on the
expiry nearest TARGET_DTE days out, at the listed strike nearest SIGMA_DIST
standard deviations below spot (using the at-the-money put's implied vol on
that expiry), and hold to expiry. A point-in-time backtest net of mainnet
costs was positive on 2024-06 -> 2026-06 BTC, which is what this run checks
against reality.

Runs on its own (.github/workflows/rule-paper.yml, dispatched by Supabase
pg_cron at 04:05, 04:35, 08:05 and 08:35 UTC), independent of the Derive
copy-trading agents, which were paused 2026-11-01. Entry is idempotent: one
row per rule per UTC day (UNIQUE rule_name, entry_date), written on the
first run inside the entry hour, so a late or repeated run can't
double-enter. Settlement uses Derive's own settlement price for the expired
instrument, so nothing depends on the watchlist feed. Prints counts only --
the repo and its Actions logs are public. See docs/btc-put-strategy.md.
"""
import math
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.derive_client import get_ticker  # noqa: E402
from config import DERIVE_BASE_URL  # noqa: E402

RULES = [
    {
        "name": "btc-put-2d-1.4sd",
        "asset": "BTC",
        "entry_hour_utc": 4,
        "target_dte_days": 2.17,
        "dte_tolerance_days": 0.6,
        "sigma_dist": 1.4,
        "size": 0.05,
    },
]

# Collateral used to read margin off the public margin calculator: a
# position's margin = this - post_initial_margin.
_MARGIN_PROBE_USDC = 100_000
_DAY_MS = 86_400_000


def _public(method, body, max_retries=4):
    for attempt in range(max_retries):
        try:
            resp = requests.post(f"{DERIVE_BASE_URL}/public/{method}", json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data["result"]
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)


def _margin_usd(instrument_name, amount):
    """Initial margin mainnet's standard-margin calculator requires for a
    short of `amount`, alone in an account."""
    r = _public("get_margin", {
        "margin_type": "SM",
        "simulated_positions": [{"instrument_name": instrument_name, "amount": str(-amount)}],
        "simulated_collaterals": [{"asset_name": "USDC", "amount": str(_MARGIN_PROBE_USDC)}],
    })
    return _MARGIN_PROBE_USDC - float(r["post_initial_margin"])


def _skip(conn, rule, entry_date, now_ms, reason):
    conn.execute(
        """
        INSERT INTO rule_paper_trades (rule_name, entry_date, status, skip_reason, asset, entry_ts)
        VALUES (?, ?, 'skipped', ?, ?, ?)
        ON CONFLICT (rule_name, entry_date) DO NOTHING
        """,
        (rule["name"], entry_date, reason, rule["asset"], now_ms),
    )
    return 0


def enter_if_due(conn, rule, now_ms=None):
    """Records today's entry for `rule` if we're inside its entry hour and
    today has no row yet. Returns 1 if a trade was entered, else 0."""
    now_ms = now_ms or int(time.time() * 1000)
    tm = time.gmtime(now_ms / 1000)
    if tm.tm_hour != rule["entry_hour_utc"]:
        return 0
    entry_date = time.strftime("%Y-%m-%d", tm)
    if conn.execute("SELECT 1 FROM rule_paper_trades WHERE rule_name = ? AND entry_date = ?", (rule["name"], entry_date)).fetchone():
        return 0

    listing = _public("get_all_instruments", {"currency": rule["asset"], "instrument_type": "option", "expired": False, "page_size": 1000})
    puts = [i for i in listing["instruments"] if i.get("is_active", True) and i["option_details"]["option_type"] == "P"]
    target_ms = now_ms + rule["target_dte_days"] * _DAY_MS
    expiries = sorted({int(i["option_details"]["expiry"]) for i in puts})
    near = [e for e in expiries if abs(e * 1000 - target_ms) <= rule["dte_tolerance_days"] * _DAY_MS]
    if not near:
        return _skip(conn, rule, entry_date, now_ms, "no expiry near the target")
    expiry = min(near, key=lambda e: abs(e * 1000 - target_ms))
    series = sorted((float(i["option_details"]["strike"]), i) for i in puts if int(i["option_details"]["expiry"]) == expiry)

    spot = float(get_ticker(series[0][1]["instrument_name"])["index_price"])
    atm = min(series, key=lambda s: abs(s[0] - spot))[1]
    atm_iv = (get_ticker(atm["instrument_name"]).get("option_pricing") or {}).get("iv")
    if not atm_iv:
        return _skip(conn, rule, entry_date, now_ms, "no implied vol at the money")
    atm_iv = float(atm_iv)

    T = (expiry * 1000 - now_ms) / (365 * _DAY_MS)
    target_strike = spot * math.exp(-rule["sigma_dist"] * atm_iv * math.sqrt(T))
    strike, inst = min(series, key=lambda s: abs(s[0] - target_strike))
    t = get_ticker(inst["instrument_name"])
    bid = float(t["best_bid_price"]) if t.get("best_bid_price") else 0.0
    if bid <= 0:
        return _skip(conn, rule, entry_date, now_ms, "no bid on the target strike")

    size = rule["size"]
    fee = float(inst["base_fee"]) + min(float(inst["taker_fee_rate"]) * size * spot, 0.125 * bid * size)
    try:
        margin = _margin_usd(inst["instrument_name"], size)
    except Exception:
        margin = None
    conn.execute(
        """
        INSERT INTO rule_paper_trades
            (rule_name, entry_date, status, asset, instrument, option_type, strike, expiry, size, entry_ts,
             spot_at_entry, atm_iv, target_strike, bid, ask, mark, bid_amount, entry_fee_usd, margin_usd)
        VALUES (?, ?, 'open', ?, ?, 'put', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (rule_name, entry_date) DO NOTHING
        """,
        (rule["name"], entry_date, rule["asset"], inst["instrument_name"], strike, expiry, size, now_ms,
         spot, atm_iv, target_strike, bid,
         float(t["best_ask_price"]) if t.get("best_ask_price") else None,
         float(t["mark_price"]) if t.get("mark_price") else None,
         float(t["best_bid_amount"]) if t.get("best_bid_amount") else None,
         fee, margin),
    )
    return 1


def settlement_price(instrument_name):
    """Derive's official settlement price for an expired option, or None
    until it's published."""
    details = _public("get_instrument", {"instrument_name": instrument_name}).get("option_details") or {}
    v = details.get("settlement_price")
    return float(v) if v not in (None, "") else None


def resolve(conn, now_ms=None):
    """Settles open rows whose option has expired, at Derive's official
    settlement price. Settlement has no fee. Returns rows resolved."""
    now_ms = now_ms or int(time.time() * 1000)
    resolved = 0
    for r in conn.execute("SELECT * FROM rule_paper_trades WHERE status = 'open' AND expiry * 1000 <= ?", (now_ms,)).fetchall():
        try:
            spot = settlement_price(r["instrument"])
        except Exception as e:
            print(f"settlement lookup failed this run, retried next run: {type(e).__name__}")
            continue
        if spot is None:
            continue  # not published yet; retry next run
        exit_value = max(0.0, r["strike"] - spot)
        pnl = (r["bid"] - exit_value) * r["size"] - r["entry_fee_usd"]
        conn.execute(
            "UPDATE rule_paper_trades SET status = 'resolved', settle_spot = ?, exit_value = ?, resolved_ts = ?, pnl_usd = ? WHERE id = ?",
            (spot, exit_value, r["expiry"] * 1000, pnl, r["id"]),
        )
        resolved += 1
    return resolved


def run_tick(conn):
    """(entered, resolved) across all rules. A failure in one rule's entry
    is caught there, so it can't stop resolution or the other rules."""
    entered = 0
    for rule in RULES:
        try:
            entered += enter_if_due(conn, rule)
        except Exception as e:
            print(f"rule paper entry failed for {rule['name']} this tick: {type(e).__name__}")
    resolved = resolve(conn)
    conn.commit()
    return entered, resolved


if __name__ == "__main__":
    from db.cloud_conn import get_live_conn

    conn = get_live_conn()
    n_entered, n_resolved = run_tick(conn)
    conn.close()
    print(f"rule paper: {n_entered} entered, {n_resolved} settled")
