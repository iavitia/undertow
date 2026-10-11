"""One-time repair for cloud options_events, companion to
scripts/repair_options_events_shift.py (local). Different mechanism,
same root cause: cloud's schema has no is_flagged/flag_reason/
anomaly_score/wallet_jump_ratio columns at all (confirmed against
db/cloud_schema.sql), so the 4-column shift that hit local SQLite
couldn't have happened independently in Postgres. Confirmed live
instead: cloud's corrupted rows hold the exact same garbage local had
pre-repair (e.g. raw_json = 'settled', a tx_status value) -- this is a
direct copy of local's already-broken values via
seed_cloud_db.backfill_new_wallets_history(), which SELECT *'d the
local row (then-corrupted) and pushed those exact field values to
cloud's same-named columns. Cloud's raw_json was never real JSON to
begin with, so there's nothing to recover from cloud itself.

Repair strategy: for each affected cloud row, find its matching
now-repaired LOCAL row via the natural key (source, source_trade_id,
wallet_address) -- confirmed live that all 14,805 affected cloud rows
have one -- and copy the repaired values across. Local must already be
repaired (scripts/repair_options_events_shift.py) before running this.

Usage:
    python scripts/repair_cloud_options_events_shift.py           # dry run
    python scripts/repair_cloud_options_events_shift.py --apply    # backs up affected rows to a local file, then writes
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DB_PATH
from db.cloud_conn import get_live_conn

COLUMNS = ["mark_price", "rfq_id", "tx_status", "trade_fee", "liquidity_role", "realized_pnl", "raw_json"]
BACKUP_PATH = Path(__file__).resolve().parent.parent / "data" / "cloud_options_events_pre_shift_repair.json"


def find_affected_cloud_rows(cloud):
    return cloud.execute(
        "SELECT id, source, source_trade_id, wallet_address, mark_price, rfq_id, tx_status, "
        "trade_fee, liquidity_role, realized_pnl, raw_json FROM options_events "
        "WHERE raw_json IS NULL OR substr(raw_json, 1, 1) != '{'"
    ).fetchall()


def repaired_values_from_local(local, source, source_trade_id, wallet_address):
    row = local.execute(
        "SELECT mark_price, rfq_id, tx_status, trade_fee, liquidity_role, realized_pnl, raw_json "
        "FROM options_events WHERE source = ? AND source_trade_id = ? AND wallet_address = ?",
        (source, source_trade_id, wallet_address),
    ).fetchone()
    return dict(row) if row else None


def dry_run(cloud, local):
    rows = find_affected_cloud_rows(cloud)
    print(f"{len(rows)} affected cloud rows found")

    change_counts = {c: 0 for c in COLUMNS}
    missing = 0
    samples = []
    for row in rows:
        repaired = repaired_values_from_local(local, row["source"], row["source_trade_id"], row["wallet_address"])
        if repaired is None:
            missing += 1
            continue
        for col in COLUMNS:
            if row[col] != repaired[col]:
                change_counts[col] += 1
        if len(samples) < 2:
            samples.append((row["id"], {c: row[c] for c in COLUMNS}, repaired))

    print(f"missing a local counterpart: {missing}")
    print("rows that would change, per column:")
    for col, n in change_counts.items():
        print(f"  {col}: {n}")
    for rid, before, after in samples:
        print(f"id={rid}\n  before: {before}\n  after:  {after}")
    return rows, missing


def apply_repair(cloud, local, rows):
    # Back up affected rows' CURRENT (pre-repair) state to a local file
    # first -- the Postgres equivalent of the local repair's sqlite file
    # copy, since there's no single-file snapshot to copy here.
    backup = [
        {
            "id": r["id"], "source": r["source"], "source_trade_id": r["source_trade_id"],
            "wallet_address": r["wallet_address"],
            **{c: r[c] for c in COLUMNS},
        }
        for r in rows
    ]
    BACKUP_PATH.write_text(json.dumps(backup, default=str))
    print(f"backed up {len(backup)} rows' pre-repair state to {BACKUP_PATH}")

    t0 = time.time()
    updates = []
    skipped = 0
    for row in rows:
        repaired = repaired_values_from_local(local, row["source"], row["source_trade_id"], row["wallet_address"])
        if repaired is None:
            skipped += 1
            continue
        updates.append((
            repaired["mark_price"], repaired["rfq_id"], repaired["tx_status"], repaired["trade_fee"],
            repaired["liquidity_role"], repaired["realized_pnl"], repaired["raw_json"], row["id"],
        ))

    cloud.executemany(
        """
        UPDATE options_events SET
            mark_price = ?, rfq_id = ?, tx_status = ?, trade_fee = ?, liquidity_role = ?, realized_pnl = ?, raw_json = ?
        WHERE id = ?
        """,
        updates,
    )
    cloud.commit()
    print(f"repaired {len(updates)} cloud rows ({skipped} skipped) in {time.time()-t0:.1f}s")


def run():
    apply = "--apply" in sys.argv

    cloud = get_live_conn()
    local = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    local.row_factory = sqlite3.Row

    print("=== dry run ===")
    rows, missing = dry_run(cloud, local)

    if not apply:
        print("\ndry run only -- rerun with --apply to write")
        cloud.close()
        local.close()
        return

    if missing:
        print(f"REFUSING to apply: {missing} row(s) have no local counterpart. Investigate before repairing.")
        cloud.close()
        local.close()
        sys.exit(1)

    print("\n=== applying repair ===")
    apply_repair(cloud, local, rows)
    cloud.close()
    local.close()


if __name__ == "__main__":
    run()
