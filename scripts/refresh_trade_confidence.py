"""Keeps wallet_edge_profile fresh for active watchlist wallets -- same
skip-if-nothing-changed pattern as scripts/refresh_qualification.py, for
the same reason: scripts/build_trade_confidence.py's full-universe
simulate_copy_trades() pass costs ~100s regardless of how many wallets
actually need updating, so the lever that matters is run FREQUENCY, not
scope.

Built after finding this table had NO scheduled refresh at all -- it was
computed once, manually, and then silently went 23+ hours stale while the
live agent kept using it to gate every newly-detected trade (see
conversation: comparing that stale snapshot to a fresh recompute found 6
buckets across 4-6 wallets had actually flipped their weak-edge
classification in that window -- no real paper trade happened to slip
through on the wrong side of a flip this time, but the gate would have
made the wrong call if one had). wallet_qualification had exactly this
gap once before refresh_qualification.py was built; this closes the same
gap for the edge-bucket gate."""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH
from scripts import build_trade_confidence


def needs_refresh(conn):
    """True if any active watchlist wallet has a trade more recent than
    wallet_edge_profile's last computed_at, or the table is empty."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM watchlist wl
        WHERE wl.is_active = 1
          AND (
              (SELECT MAX(computed_at) FROM wallet_edge_profile) IS NULL
              OR (SELECT MAX(timestamp) FROM options_events WHERE wallet_address = wl.wallet_address)
                 > (SELECT MAX(computed_at) FROM wallet_edge_profile)
          )
        """
    ).fetchone()
    return row["n"] > 0


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if not needs_refresh(conn):
        print("no active watchlist wallet has new trades since wallet_edge_profile was last computed -- skipping")
        conn.close()
        return
    conn.close()  # build_trade_confidence.run() opens its own connection

    t0 = time.time()
    build_trade_confidence.run()
    print(f"edge-profile refresh took {time.time()-t0:.1f}s")


if __name__ == "__main__":
    run()
