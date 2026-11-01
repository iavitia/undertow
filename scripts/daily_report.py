"""Daily report: the Hyperliquid paper strategy and the wallet recorder's
health, from the cloud DB.

The Derive pipeline (copy-trading agents, testnet orders, the BTC put
paper run) was retired on 2026-11-01 and its tables dropped, so this
covers Hyperliquid only. Counts only, never addresses.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.cloud_conn import get_live_conn

WINDOW_HOURS = 24
DB_LIMIT_MB = 500  # Supabase free tier


def hl_paper_summary(conn):
    """The Hyperliquid perp strategy's paper equity (scripts/hl_paper.py)."""
    rows = conn.execute("SELECT * FROM hl_paper_strategy ORDER BY strategy, day").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["strategy"], []).append(r)
    res = []
    for name, rs in out.items():
        peak, dd = rs[0]["equity"], 0.0
        for r in rs:
            peak = max(peak, r["equity"])
            dd = min(dd, r["equity"] / peak - 1)
        days = (rs[-1]["day"] - rs[0]["day"]) / 86_400_000
        res.append({"name": name, "days": days, "ret": rs[-1]["equity"] / rs[0]["equity"] - 1, "maxdd": dd,
                    "state": "trend (long perp)" if rs[-1]["in_trend"] else "carry", "trend_pos": rs[-1]["trend_pos"],
                    "fees": sum(r["fee_pct"] for r in rs), "since": time.strftime("%Y-%m-%d", time.gmtime(rs[0]["day"] / 1000))})
    return res


def recorder_summary(conn, since_ms):
    """Leaderboard snapshots, tracked wallets and fills (scripts/hl_recorder.py)."""
    snap = conn.execute(
        "SELECT COUNT(DISTINCT snapshot_date) AS days, MAX(snapshot_date) AS last FROM hl_leaderboard_snapshots"
    ).fetchone()
    wallets = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM hl_tracked_wallets GROUP BY status"
    ).fetchall()}
    fills = conn.execute(
        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE time >= ?) AS recent, MIN(time) AS first FROM hl_fills",
        (since_ms,),
    ).fetchone()
    db_bytes = conn.execute("SELECT pg_database_size(current_database()) AS b").fetchone()["b"]
    return snap, wallets, fills, db_bytes / 1e6


def run(window_hours=WINDOW_HOURS):
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - window_hours * 3600 * 1000

    conn = get_live_conn()
    hl = hl_paper_summary(conn)
    snap, wallets, fills, db_mb = recorder_summary(conn, since_ms)
    conn.close()

    print(f"=== Daily report -- trailing {window_hours}h ===")

    for h in hl:
        print(f"\n-- Hyperliquid paper strategy: {h['name']} (since {h['since']}, no orders) --")
        print(f"  {h['days']:.0f} day(s): return {h['ret']:+.2%} | max drawdown {h['maxdd']:+.2%} | fees paid {h['fees']:.3%} | now in {h['state']}"
              + (f" at {h['trend_pos']:.2f}x" if h['state'].startswith('trend') else ""))
        print("  backtest reference (2023-06 -> 2026-10): ~+12.8%/yr, max drawdown -12%; choppy years ~+2-4%")
    if not hl:
        print("\n-- Hyperliquid paper strategy: no days recorded yet --")

    print("\n-- Hyperliquid recorder --")
    print(f"  leaderboard: {snap['days']} daily snapshot(s), latest {snap['last']}")
    print(f"  wallets: {wallets.get('tracking', 0)} tracking, {wallets.get('too_active', 0)} too active (not stored)")
    first = time.strftime("%Y-%m-%d", time.gmtime(fills["first"] / 1000)) if fills["first"] else "n/a"
    print(f"  fills: {fills['total']:,} stored since {first}, {fills['recent']:,} in the last {window_hours}h")
    print(f"  database: {db_mb:.0f} MB of {DB_LIMIT_MB} MB")


if __name__ == "__main__":
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else WINDOW_HOURS
    run(hours)
