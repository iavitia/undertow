"""One-time repair for the options_events column-shift bug found by the
wheel research track (docs/wheel-phase1a.md section 1.5) and confirmed
directly against the live DB this session: 985,259 rows (286,164 BTC +
699,095 ETH; SOL untouched -- all ingested after the shift window) have
mark_price/rfq_id/tx_status/trade_fee/liquidity_role/realized_pnl/raw_json
shifted out of place, landing in is_flagged/flag_reason/anomaly_score/
wallet_jump_ratio instead. raw_json itself got bumped to wallet_jump_ratio,
so the real JSON for an affected row lives in whichever of those two
columns actually starts with '{' -- confirmed every single affected row
has it in one or the other (zero orphans with no recoverable JSON).

This also fully explains a previously-separate-looking mystery: all
183,116 rows with a UUID-shaped realized_pnl (scripts/build_leaderboard.py
already had a defensive coercion for this) turn out to be a subset of
these same shift-affected rows -- not a distinct bug. The repair derives
every column fresh from the parsed JSON rather than assuming one fixed
offset, so it's correct regardless of how many different historical
corruption patterns produced the various columns' garbage.

The four receiving columns (is_flagged/flag_reason/anomaly_score/
wallet_jump_ratio) are scripts/run_anomaly_detection.py's own output
columns, not derived from trade JSON at all -- repair resets them to
their "not yet computed" defaults (0/NULL/NULL/NULL) rather than
inventing values, since the detector needs to be rerun against correct
data anyway to mean anything.

Usage:
    python scripts/repair_options_events_shift.py           # dry run, writes nothing
    python scripts/repair_options_events_shift.py --apply    # backs up, then writes
"""
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DB_PATH

COLUMNS = ["mark_price", "rfq_id", "tx_status", "trade_fee", "liquidity_role", "realized_pnl", "raw_json"]


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def derive_repaired_values(row):
    """row: sqlite3.Row with id, raw_json, wallet_jump_ratio (plus the 6
    other currently-corrupted columns, for comparison). Returns a dict of
    the 11 correct column values, or None if no valid JSON exists on this
    row (shouldn't happen -- confirmed zero orphans live, but checked
    rather than assumed)."""
    raw = row["raw_json"]
    js = raw if (raw and raw.startswith("{")) else row["wallet_jump_ratio"]
    if not js or not js.startswith("{"):
        return None
    j = json.loads(js)
    return {
        "mark_price": _num(j.get("mark_price")),
        "rfq_id": j.get("rfq_id"),
        "tx_status": j.get("tx_status") or j.get("batch_status"),
        "trade_fee": _num(j.get("trade_fee")),
        "liquidity_role": j.get("liquidity_role"),
        "realized_pnl": _num(j.get("realized_pnl")),
        "raw_json": js,
        "is_flagged": 0,
        "flag_reason": None,
        "anomaly_score": None,
        "wallet_jump_ratio": None,
    }


def find_affected_rows(conn):
    return conn.execute(
        "SELECT id, raw_json, wallet_jump_ratio, mark_price, rfq_id, tx_status, trade_fee, "
        "liquidity_role, realized_pnl FROM options_events "
        "WHERE raw_json IS NULL OR substr(raw_json, 1, 1) != '{'"
    ).fetchall()


def dry_run(conn):
    rows = find_affected_rows(conn)
    print(f"{len(rows)} affected rows found")

    change_counts = {c: 0 for c in COLUMNS}
    orphans = 0
    samples = []
    for row in rows:
        repaired = derive_repaired_values(row)
        if repaired is None:
            orphans += 1
            continue
        for col in COLUMNS:
            if row[col] != repaired[col]:
                change_counts[col] += 1
        if len(samples) < 3:
            samples.append((row["id"], {c: row[c] for c in COLUMNS}, {c: repaired[c] for c in COLUMNS}))

    print(f"orphans (no recoverable JSON): {orphans}")
    print("rows that would change, per column:")
    for col, n in change_counts.items():
        print(f"  {col}: {n}")
    print()
    for rid, before, after in samples:
        print(f"id={rid}")
        print(f"  before: {before}")
        print(f"  after:  {after}")
    return rows, orphans


def apply_repair(conn, rows):
    t0 = time.time()
    updates = []
    skipped = 0
    for row in rows:
        repaired = derive_repaired_values(row)
        if repaired is None:
            skipped += 1
            continue
        updates.append((
            repaired["mark_price"], repaired["rfq_id"], repaired["tx_status"], repaired["trade_fee"],
            repaired["liquidity_role"], repaired["realized_pnl"], repaired["raw_json"],
            repaired["is_flagged"], repaired["flag_reason"], repaired["anomaly_score"], repaired["wallet_jump_ratio"],
            row["id"],
        ))

    conn.executemany(
        """
        UPDATE options_events SET
            mark_price = ?, rfq_id = ?, tx_status = ?, trade_fee = ?, liquidity_role = ?, realized_pnl = ?,
            raw_json = ?, is_flagged = ?, flag_reason = ?, anomaly_score = ?, wallet_jump_ratio = ?
        WHERE id = ?
        """,
        updates,
    )
    conn.commit()
    print(f"repaired {len(updates)} rows ({skipped} skipped, no recoverable JSON) in {time.time()-t0:.1f}s")


def run():
    apply = "--apply" in sys.argv

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=== dry run ===")
    rows, orphans = dry_run(conn)

    if not apply:
        print()
        print("dry run only -- rerun with --apply to write (will back up data/undertow.db first)")
        conn.close()
        return

    if orphans:
        print(f"REFUSING to apply: {orphans} row(s) have no recoverable JSON. Investigate before repairing.")
        conn.close()
        sys.exit(1)

    backup_path = DB_PATH.parent / f"undertow.db.pre_shift_repair.{int(time.time())}.bak"
    conn.close()  # release the file before copying
    shutil.copy2(DB_PATH, backup_path)
    print(f"\nbacked up to {backup_path}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    print("\n=== applying repair ===")
    apply_repair(conn, rows)
    conn.close()


if __name__ == "__main__":
    run()
