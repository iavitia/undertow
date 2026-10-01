"""Keeps wallet_qualification fresh for active watchlist wallets without
paying scripts/build_wallet_qualification.py's ~100s full-universe cost on
every run for no reason (see conversation -- the expensive part,
simulate_copy_trades(), builds a market-wide "next other-wallet print"
price index needed for accurate copy-trade P&L, which isn't something
that can be scoped down to just the wallets that changed; there's no
per-wallet-cheap path with the current architecture, so scoping the
*computation* to only changed wallets wouldn't actually save much). The
lever that actually matters is run FREQUENCY, not scope: this checks
first whether anything active-watchlist-relevant has changed since the
last full requalification, and skips the whole ~100s pass entirely if
not (protects the genuinely quiet stretches -- overnight, weekends) --
but runs the same proven, already-tested batch job when something has,
which also refreshes the full wallet universe and surfaces newly-
qualifying candidates as a side effect, not just watchlist wallets.

Meant to run on its own schedule, separate from and slower than
scripts/run_watchlist_agent.py's 15-minute live-mirroring tick. Adding
~100s to that tick would meaningfully eat its budget and stack with
everything else it already does (live ingest, position detection,
resolution) -- exactly the kind of concurrent load that caused a real
"database is locked" crash earlier in this project's history. An hourly
cadence keeps qualification tiers no more than ~1h stale, short relative
to how long a position actually takes to resolve (days)."""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH
from scripts import build_wallet_qualification, sync_watchlist


def needs_refresh(conn):
    """True if any active watchlist wallet has a trade more recent than
    its last wallet_qualification.computed_at, or has no qualification
    row at all."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM watchlist wl
        LEFT JOIN wallet_qualification q ON q.wallet_address = wl.wallet_address
        WHERE wl.is_active = 1
          AND (
              q.wallet_address IS NULL
              OR q.computed_at < (SELECT MAX(timestamp) FROM options_events WHERE wallet_address = wl.wallet_address)
          )
        """
    ).fetchone()
    return row["n"] > 0


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if not needs_refresh(conn):
        print("no active watchlist wallet has new trades since its last qualification -- skipping")
        conn.close()
        return
    conn.close()  # build_wallet_qualification.run() opens its own connection

    t0 = time.time()
    build_wallet_qualification.run()
    print(f"requalification pass took {time.time()-t0:.1f}s")
    sync_watchlist.run()


if __name__ == "__main__":
    run()
