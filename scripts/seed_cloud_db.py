"""One-time (and re-runnable) setup for the hosted "live" Postgres DB --
see db/cloud_schema.sql's header and the hosting plan for why only a
small slice of the project lives here.

1. Creates the live-path tables (db/cloud_schema.sql) if they don't exist.
2. Copies wallets/watchlist/wallet_qualification/wallet_edge_profile rows
   from the local SQLite archive into the cloud DB -- but ONLY for wallets
   on the ACTIVE watchlist, not the full ~7,600-wallet universe. This is
   deliberate, not an oversight: scripts/run_watchlist_agent.py's
   find_new_candidates() only ever looks up ratings for wallets with
   `watchlist.is_active = 1` -- every other wallet's qualification/edge
   data is irrelevant to live execution, and syncing it all would bloat
   the cloud DB for no reason.

Safe to re-run: wallets/watchlist/wallet_qualification/wallet_edge_profile
rows are upserted (ON CONFLICT DO UPDATE), so running this again after a
local requalification just refreshes the cloud copy -- though
scripts/sync_watchlist_to_cloud.py is the intended way to do that
day-to-day; this script's job is standing the cloud DB up the first time."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DB_PATH
from db.cloud_conn import get_cloud_conn


def create_schema(cloud):
    schema_path = Path(__file__).resolve().parent.parent / "db" / "cloud_schema.sql"
    sql = schema_path.read_text()
    # psycopg can run a multi-statement script in one .execute() call;
    # CloudConnection.execute() doesn't special-case this, so go straight
    # at the underlying connection for just this one setup step.
    cloud._conn.execute(sql)
    cloud.commit()


def sync_live_wallets(local, cloud):
    """Pushes wallets/watchlist/wallet_qualification/wallet_edge_profile
    for the current active watchlist from local SQLite to the cloud DB.
    Shared by this script's initial seed and
    scripts/sync_watchlist_to_cloud.py's day-to-day refresh -- same
    operation, different occasions, no reason for two copies."""
    active = local.execute("SELECT wallet_address FROM watchlist WHERE is_active = 1").fetchall()
    addresses = [r["wallet_address"] for r in active]
    print(f"{len(addresses)} active watchlist wallets to seed")
    if not addresses:
        return

    placeholders = ",".join("?" for _ in addresses)

    wallets = local.execute(f"SELECT * FROM wallets WHERE address IN ({placeholders})", addresses).fetchall()
    for w in wallets:
        cloud.execute(
            """
            INSERT INTO wallets (address, chain, first_seen, label, alias)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (address) DO UPDATE SET
                chain = excluded.chain, first_seen = excluded.first_seen,
                label = excluded.label, alias = excluded.alias
            """,
            (w["address"], w["chain"], w["first_seen"], w["label"], w["alias"]),
        )
    print(f"  wallets: {len(wallets)}")

    watchlist_rows = local.execute(f"SELECT * FROM watchlist WHERE wallet_address IN ({placeholders})", addresses).fetchall()
    for r in watchlist_rows:
        cloud.execute(
            """
            INSERT INTO watchlist
                (wallet_address, added_at, source, added_reason, is_active,
                 removed_at, removed_reason, last_checked_ts, live_last_checked_ts, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (wallet_address) DO UPDATE SET
                added_at = excluded.added_at, source = excluded.source,
                added_reason = excluded.added_reason, is_active = excluded.is_active,
                removed_at = excluded.removed_at, removed_reason = excluded.removed_reason,
                last_checked_ts = excluded.last_checked_ts,
                live_last_checked_ts = excluded.live_last_checked_ts, notes = excluded.notes
            """,
            (
                r["wallet_address"], r["added_at"], r["source"], r["added_reason"], r["is_active"],
                r["removed_at"], r["removed_reason"], r["last_checked_ts"], r["live_last_checked_ts"], r["notes"],
            ),
        )
    print(f"  watchlist: {len(watchlist_rows)}")

    qual_rows = local.execute(f"SELECT * FROM wallet_qualification WHERE wallet_address IN ({placeholders})", addresses).fetchall()
    for r in qual_rows:
        cloud.execute(
            """
            INSERT INTO wallet_qualification
                (wallet_address, total_n, tier, qualified_at_n, qualified_at_ts,
                 pre_qualification_n, pre_qualification_win_rate, pre_qualification_wilson_low, pre_qualification_total_return_pct,
                 forward_n, forward_win_rate, forward_wilson_low, forward_total_return_pct, forward_median_return_pct,
                 risk_discipline_score, rfq_pct, sizing_cv, is_likely_bot, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (wallet_address) DO UPDATE SET
                total_n = excluded.total_n, tier = excluded.tier,
                qualified_at_n = excluded.qualified_at_n, qualified_at_ts = excluded.qualified_at_ts,
                pre_qualification_n = excluded.pre_qualification_n,
                pre_qualification_win_rate = excluded.pre_qualification_win_rate,
                pre_qualification_wilson_low = excluded.pre_qualification_wilson_low,
                pre_qualification_total_return_pct = excluded.pre_qualification_total_return_pct,
                forward_n = excluded.forward_n, forward_win_rate = excluded.forward_win_rate,
                forward_wilson_low = excluded.forward_wilson_low,
                forward_total_return_pct = excluded.forward_total_return_pct,
                forward_median_return_pct = excluded.forward_median_return_pct,
                risk_discipline_score = excluded.risk_discipline_score, rfq_pct = excluded.rfq_pct,
                sizing_cv = excluded.sizing_cv, is_likely_bot = excluded.is_likely_bot,
                computed_at = excluded.computed_at
            """,
            (
                r["wallet_address"], r["total_n"], r["tier"], r["qualified_at_n"], r["qualified_at_ts"],
                r["pre_qualification_n"], r["pre_qualification_win_rate"], r["pre_qualification_wilson_low"],
                r["pre_qualification_total_return_pct"], r["forward_n"], r["forward_win_rate"],
                r["forward_wilson_low"], r["forward_total_return_pct"], r["forward_median_return_pct"],
                r["risk_discipline_score"], r["rfq_pct"], r["sizing_cv"], r["is_likely_bot"], r["computed_at"],
            ),
        )
    print(f"  wallet_qualification: {len(qual_rows)}")

    edge_rows = local.execute(f"SELECT * FROM wallet_edge_profile WHERE wallet_address IN ({placeholders})", addresses).fetchall()
    for r in edge_rows:
        cloud.execute(
            """
            INSERT INTO wallet_edge_profile
                (wallet_address, asset, option_type, entry_side, n, win_rate, wilson_low,
                 median_return_pct, avg_return_pct, return_lcb, top1_gain_share, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (wallet_address, asset, option_type, entry_side) DO UPDATE SET
                n = excluded.n, win_rate = excluded.win_rate, wilson_low = excluded.wilson_low,
                median_return_pct = excluded.median_return_pct, avg_return_pct = excluded.avg_return_pct,
                return_lcb = excluded.return_lcb, top1_gain_share = excluded.top1_gain_share,
                computed_at = excluded.computed_at
            """,
            (
                r["wallet_address"], r["asset"], r["option_type"], r["entry_side"], r["n"], r["win_rate"],
                r["wilson_low"], r["median_return_pct"], r["avg_return_pct"], r["return_lcb"],
                r["top1_gain_share"], r["computed_at"],
            ),
        )
    print(f"  wallet_edge_profile: {len(edge_rows)}")

    cloud.commit()


def run():
    local = sqlite3.connect(DB_PATH)
    local.row_factory = sqlite3.Row
    cloud = get_cloud_conn()

    print("creating cloud schema (idempotent)...")
    create_schema(cloud)

    print("seeding live-path tables from local DB...")
    sync_live_wallets(local, cloud)

    local.close()
    cloud.close()
    print("done")


if __name__ == "__main__":
    run()
