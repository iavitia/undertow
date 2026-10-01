"""Seeds/updates the watchlist from wallet_qualification (see
analysis/qualification.py). Deliberately not a live join at query time
(see db/schema.sql's comment on the watchlist table) -- this snapshots the
current qualifying wallets into a table the agent can keep tracking even
if a wallet's tier later degrades.

Two ways onto the watchlist:
  1. tier IN ('validated', 'high_confidence') -- real, sufficient forward
     evidence (n >= MIN_FORWARD_N positions genuinely traded AFTER the
     wallet first qualified, not just a good-looking full-history number).
     Added regardless of recent activity, same as before -- the historical
     evidence is the point, and several watchlist wallets go quiet for
     months and stay on anyway (removal is a separate, deliberate decision,
     see below).
  2. tier == 'provisional' AND traded within ACTIVE_WITHIN_DAYS -- cleared
     the qualification bar but doesn't have enough forward data yet to
     call it validated. This is the path that catches a wallet like
     drift-vault-68 automatically, without a human having to spot it and
     add it by hand first -- the entire reason wallet_qualification is now
     computed for every wallet in the batch job, not just ones someone
     happens to look at (see conversation).

tier IN ('insufficient_data', 'does_not_qualify', 'qualified_but_faded')
never qualifies for auto-add -- no evidence, never cleared the bar, or
looked good early and didn't hold up, respectively.

Removal (see conversation, added after iron-grove-22 sat on the watchlist
for months after fading -- it had been auto-added under the old, now-
superseded passes_screen system and this script previously only ever
added, never removed, so nothing ever caught the degrade): any currently-
active wallet whose CURRENT tier is qualified_but_faded or does_not_qualify
gets deactivated. Both mean the same thing from a copier's standpoint --
this wallet is not currently worth following -- they just differ in
whether it ever cleared the bar in the first place. This only checks
whatever tier is already sitting in wallet_qualification; it does not
itself recompute anything (see scripts/refresh_qualification.py, which
requalifies stale wallets and then calls this)."""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH

ACTIVE_WITHIN_DAYS = 30
DISQUALIFYING_TIERS = ("qualified_but_faded", "does_not_qualify")


def deactivate_faded(conn):
    """Removes any active watchlist wallet whose current tier has dropped
    into a disqualifying bucket. Returns the list of aliases removed."""
    now_ms = int(time.time() * 1000)
    placeholders = ", ".join("?" * len(DISQUALIFYING_TIERS))
    rows = conn.execute(
        f"""
        SELECT wl.wallet_address, w.alias, q.tier, q.forward_wilson_low, q.forward_total_return_pct
        FROM watchlist wl
        LEFT JOIN wallets w ON w.address = wl.wallet_address
        JOIN wallet_qualification q ON q.wallet_address = wl.wallet_address
        WHERE wl.is_active = 1 AND q.tier IN ({placeholders})
        """,
        DISQUALIFYING_TIERS,
    ).fetchall()

    removed = []
    for r in rows:
        wilson_str = f"{r['forward_wilson_low']*100:.1f}%" if r["forward_wilson_low"] is not None else "n/a"
        ret_str = f"{r['forward_total_return_pct']*100:.1f}%" if r["forward_total_return_pct"] is not None else "n/a"
        reason = f"requalified as {r['tier']}: forward_wilson_low={wilson_str}, forward_total_return_pct={ret_str}"
        conn.execute(
            "UPDATE watchlist SET is_active = 0, removed_at = ?, removed_reason = ? WHERE wallet_address = ?",
            (now_ms, reason, r["wallet_address"]),
        )
        removed.append(r["alias"] or r["wallet_address"])

    conn.commit()
    return removed


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    now_ms = int(time.time() * 1000)
    active_cutoff_ts = now_ms - ACTIVE_WITHIN_DAYS * 86400000

    candidates = conn.execute(
        """
        SELECT q.wallet_address, w.alias, q.tier, q.total_n, q.forward_n, q.forward_wilson_low,
               q.forward_total_return_pct, q.risk_discipline_score,
               (SELECT MAX(timestamp) FROM options_events WHERE wallet_address = q.wallet_address) AS last_trade_ts
        FROM wallet_qualification q
        LEFT JOIN wallets w ON w.address = q.wallet_address
        WHERE q.tier IN ('validated', 'high_confidence', 'provisional') AND q.is_likely_bot = 0
        ORDER BY q.forward_wilson_low DESC
        """
    ).fetchall()

    existing = {r["wallet_address"] for r in conn.execute("SELECT wallet_address FROM watchlist").fetchall()}
    added = []
    for c in candidates:
        if c["wallet_address"] in existing:
            continue

        if c["tier"] in ("validated", "high_confidence"):
            eligible = True
        else:  # provisional -- needs recent activity too
            eligible = c["last_trade_ts"] is not None and c["last_trade_ts"] >= active_cutoff_ts
        if not eligible:
            continue

        wilson_str = f"{c['forward_wilson_low']*100:.1f}%" if c["forward_wilson_low"] is not None else "n/a"
        # risk_discipline_score is None when a wallet is missing any of its
        # three inputs (tail_ratio, rfq_pct, sizing_cv) -- e.g. too few
        # trades for a meaningful sizing_cv. Not previously hit because no
        # validated/high_confidence wallet had happened to be missing one
        # until the 2024-06-01 backfill surfaced several (see conversation).
        risk_str = f"{c['risk_discipline_score']:.0f}" if c["risk_discipline_score"] is not None else "n/a"
        reason = (
            f"auto-added ({c['tier']}): total_n={c['total_n']}, forward_n={c['forward_n']}, "
            f"forward_wilson_low={wilson_str}, "
            f"forward_total_return_pct={(c['forward_total_return_pct'] or 0)*100:.1f}%, "
            f"risk_discipline_score={risk_str}"
        )
        conn.execute(
            """
            INSERT INTO watchlist (wallet_address, added_at, source, added_reason, is_active, last_checked_ts)
            VALUES (?, ?, 'auto', ?, 1, ?)
            """,
            (c["wallet_address"], now_ms, reason, now_ms),
        )
        added.append(c["alias"] or c["wallet_address"])

    conn.commit()
    removed = deactivate_faded(conn)
    active_count = conn.execute("SELECT COUNT(*) FROM watchlist WHERE is_active = 1").fetchone()[0]
    conn.close()

    if added:
        print(f"added {len(added)} wallet(s) to watchlist: {', '.join(added)}")
    else:
        print("no new wallets to add")
    if removed:
        print(f"removed {len(removed)} faded wallet(s) from watchlist: {', '.join(removed)}")
    else:
        print("no wallets to remove")
    print(f"watchlist now has {active_count} active wallet(s)")


if __name__ == "__main__":
    run()
