"""Run by hand after a local wallet requalification
(scripts/build_wallet_qualification.py / build_trade_confidence.py /
scripts/sync_watchlist.py) to push the refreshed watchlist and ratings up
to the hosted cloud DB -- see the hosting plan: this is the actual
"limit how often we re-analyze wallets" lever. Requalification frequency
stays a matter of local convenience (run it whenever); the cloud live
path is simply unaffected until this is run again.

Thin wrapper around scripts/seed_cloud_db.py's sync_live_wallets() --
same operation as the initial seed, just without recreating the schema."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DB_PATH
from db.cloud_conn import get_cloud_conn
from scripts.seed_cloud_db import sync_live_wallets


def run():
    local = sqlite3.connect(DB_PATH)
    local.row_factory = sqlite3.Row
    cloud = get_cloud_conn()

    sync_live_wallets(local, cloud)

    local.close()
    cloud.close()
    print("synced")


if __name__ == "__main__":
    run()
