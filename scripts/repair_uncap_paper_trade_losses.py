"""One-off repair: restores the real (uncapped) settlement return on every
historical paper_trades row the retired -100% naked-short loss floor
clamped (loss_capped = 1) -- see docs/quant-review-2026-10.md and
scripts/resolve_paper_trades.py. exit_price was always stored unclamped,
so the true return is just recomputed from entry_price/exit_price; the
row's notes records the capped value it replaces, and loss_capped is
cleared so nothing downstream keeps treating it as floored.

Dry run by default (prints what would change). Pass --apply to write.
Runs against whichever DB db.cloud_conn.get_live_conn() selects -- the
cloud Postgres when CLOUD_DATABASE_URL is set, local SQLite otherwise.
Fees are deliberately not back-applied here: this only undoes the floor."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: F401 -- loads .env (CLOUD_DATABASE_URL), same as every other live script does transitively
from db.cloud_conn import get_live_conn


def run(apply=False):
    conn = get_live_conn()
    print(f"target: {'cloud Postgres' if os.environ.get('CLOUD_DATABASE_URL') else 'local SQLite'}")
    rows = conn.execute(
        "SELECT id, instrument, entry_price, exit_price, return_pct, notes FROM paper_trades "
        "WHERE loss_capped = 1 AND side = 'sell' AND entry_price > 0 AND exit_price IS NOT NULL"
    ).fetchall()

    now_ms = int(time.time() * 1000)
    capped_sum = uncapped_sum = 0.0
    for r in rows:
        uncapped = (r["entry_price"] - r["exit_price"]) / r["entry_price"]
        capped_sum += r["return_pct"]
        uncapped_sum += uncapped
        print(f"  id={r['id']} {r['instrument']}: return_pct {r['return_pct']:.3f} -> {uncapped:.3f}")
        if apply:
            note = f"uncapped at {now_ms}: was {r['return_pct']:.4f} under retired -100% loss floor"
            notes = f"{r['notes']}; {note}" if r["notes"] else note
            conn.execute(
                "UPDATE paper_trades SET return_pct = ?, loss_capped = 0, notes = ? WHERE id = ?",
                (uncapped, notes, r["id"]),
            )

    if apply:
        conn.commit()
    conn.close()
    verb = "repaired" if apply else "would repair (dry run, pass --apply to write)"
    print(f"{verb} {len(rows)} row(s): sum of return_pct {capped_sum:.2f} -> {uncapped_sum:.2f}")


if __name__ == "__main__":
    run(apply="--apply" in sys.argv)
