"""Manual/ad-hoc entry point for scripts.run_live_agent.reconcile_open_orders
-- run_live_agent.py now calls this itself every tick, so this script is
for catching up a backlog (like the one this was built to fix: 42 of 43
real option orders placed over 48h turned out to be neither filled nor
resting on Derive, with nothing ever having checked) or for a manual
spot-check outside the normal tick cadence."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DERIVE_SUBACCOUNT_ID, DERIVE_SUBACCOUNT_ID_FAST, DERIVE_SUBACCOUNT_ID_SOL
from db.cloud_conn import get_live_conn
from scripts.run_live_agent import reconcile_open_orders

SUBACCOUNT_IDS = [
    sid for sid in (DERIVE_SUBACCOUNT_ID, DERIVE_SUBACCOUNT_ID_SOL, DERIVE_SUBACCOUNT_ID_FAST) if sid is not None
]


def run():
    now_ms = int(time.time() * 1000)
    conn = get_live_conn()
    total_confirmed = total_resting = total_phantom = 0
    for sid in SUBACCOUNT_IDS:
        confirmed, still_resting, phantom = reconcile_open_orders(conn, sid, now_ms)
        print(f"subaccount {sid}: {confirmed} confirmed real, {still_resting} still resting, {phantom} never filled")
        total_confirmed += confirmed
        total_resting += still_resting
        total_phantom += phantom
    conn.close()
    print(f"\ntotal: {total_confirmed} confirmed, {total_resting} still resting, {total_phantom} flipped to never_filled")


if __name__ == "__main__":
    run()
