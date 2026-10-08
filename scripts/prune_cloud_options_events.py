"""Weekly step 2 of 2, run only after scripts/pull_cloud_options_events.py
has archived cloud's current state into local -- see that script's
docstring and the conversation this pair implements.

Deletes options_events rows for wallets NOT on the active watchlist from
the CLOUD db specifically (never local). Safe to do, not just
convenient: scripts/run_watchlist_agent.py's find_new_candidates() (the
only thing that reads options_events history for correctness -- the
"first-ever-trade" check) only ever queries rows for the active
watchlist's own wallets, one at a time. A non-watchlisted wallet's
history is never consulted by the live path at all; it only exists in
cloud because the ingest pulls the whole market, for the sake of this
weekly local catch-up. Watchlisted wallets' own rows are never touched
here, at any age -- pruning those would break "first-ever-trade"
detection for the only wallets that check actually runs against.

Refuses to delete anything it can't first verify is already safely
archived locally (by exact (source, source_trade_id, wallet_address)
match) -- a real safety check, not just running pull first and hoping:
if pull silently failed or missed rows, this aborts instead of losing
data that only ever existed in cloud."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DB_PATH
from db.cloud_conn import get_cloud_conn


def run():
    local_conn = sqlite3.connect(DB_PATH)
    local_conn.row_factory = sqlite3.Row
    cloud_conn = get_cloud_conn()

    active_wallets = {
        r["wallet_address"] for r in cloud_conn.execute("SELECT wallet_address FROM watchlist WHERE is_active = 1").fetchall()
    }
    print(f"{len(active_wallets)} active watchlist wallets -- their rows are never pruned")

    candidates = cloud_conn.execute(
        "SELECT id, source, source_trade_id, wallet_address FROM options_events"
    ).fetchall()
    to_prune = [r for r in candidates if r["wallet_address"] not in active_wallets]
    print(f"{len(candidates)} total cloud rows, {len(to_prune)} candidates for pruning (non-watchlisted wallets)")

    if not to_prune:
        print("nothing to prune")
        local_conn.close()
        cloud_conn.close()
        return

    unverified = []
    for r in to_prune:
        found = local_conn.execute(
            "SELECT 1 FROM options_events WHERE source = ? AND source_trade_id = ? AND wallet_address = ?",
            (r["source"], r["source_trade_id"], r["wallet_address"]),
        ).fetchone()
        if found is None:
            unverified.append(r)

    if unverified:
        print(f"REFUSING to prune -- {len(unverified)} of {len(to_prune)} candidate rows aren't confirmed in local yet.")
        print("Run scripts/pull_cloud_options_events.py first. First few missing:")
        for r in unverified[:5]:
            print(f"  id={r['id']} wallet={r['wallet_address']} source_trade_id={r['source_trade_id']}")
        local_conn.close()
        cloud_conn.close()
        return

    ids = [r["id"] for r in to_prune]
    deleted = 0
    for i in range(0, len(ids), 500):
        batch = ids[i : i + 500]
        placeholders = ",".join("?" for _ in batch)
        cloud_conn.execute(f"DELETE FROM options_events WHERE id IN ({placeholders})", batch)
        deleted += len(batch)
    cloud_conn.commit()

    local_conn.close()
    cloud_conn.close()
    print(f"done -- pruned {deleted} non-watchlisted rows from cloud, all verified present locally first")


if __name__ == "__main__":
    run()
