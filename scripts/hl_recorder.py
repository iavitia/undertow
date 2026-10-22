"""Daily Hyperliquid recorder: builds the point-in-time history that wallet
research needs and the public API can't provide.

The API only offers a leaderboard of today's accounts (survivors) and each
address's most recent ~10k fills, which is hours of history for an active
bot. Judging wallets from that alone selects lucky survivors. So this
records it forward, once a day:

  1. Leaderboard snapshot: every account with at least SNAPSHOT_MIN_ACCOUNT
     of equity and SNAPSHOT_MIN_MONTH_VLM of 30-day volume (~6.6k rows a day
     in 2026-10).
  2. Tracked wallets: NEW_WALLETS_PER_DAY are added each day, chosen AT
     RANDOM from accounts with at least TRACK_MIN_ACCOUNT / TRACK_MIN_MONTH_VLM,
     up to MAX_TRACKED. They are picked by size and activity, never by P&L,
     so losers are tracked too and later evaluation can be point-in-time.
  3. Their fills: the last 30 days on a wallet's first fetch, then only new
     fills each run. A wallet with more than TOO_ACTIVE_FILLS fills in its
     first 30 days is high-frequency (market-making or HFT, not something
     to clone) and is marked 'too_active' with no fills stored.

Runs from its own workflow (.github/workflows/hl-recorder.yml), dispatched
daily by Supabase pg_cron. The repo is public, so it prints counts only,
never addresses.
"""
import random
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.cloud_conn import get_live_conn  # noqa: E402
from scripts import hl_paper  # noqa: E402

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

SNAPSHOT_MIN_ACCOUNT = 25_000
SNAPSHOT_MIN_MONTH_VLM = 500_000
TRACK_MIN_ACCOUNT = 50_000
TRACK_MIN_MONTH_VLM = 1_000_000
NEW_WALLETS_PER_DAY = 20
MAX_TRACKED = 200
FIRST_FETCH_DAYS = 30
TOO_ACTIVE_FILLS = 3000
PAGE = 2000  # max fills the API returns per request

_DAY_MS = 86_400_000


def _get(url, max_retries=4):
    for attempt in range(max_retries):
        try:
            r = requests.get(url, timeout=90)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)


def _info(body, max_retries=4):
    for attempt in range(max_retries):
        try:
            r = requests.post(INFO_URL, json=body, timeout=60)
            if r.status_code == 429:
                raise requests.exceptions.ConnectionError("rate limited")
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))


def _window(row, name):
    for w, d in row.get("windowPerformances", []):
        if w == name:
            return d
    return {}


def _f(d, key):
    v = d.get(key)
    return float(v) if v not in (None, "") else None


def snapshot_leaderboard(conn, today):
    """Stores today's filtered leaderboard. Returns the full row list (for
    picking tracked wallets) and the number of rows stored."""
    rows = _get(LEADERBOARD_URL).get("leaderboardRows", [])
    out = []
    for r in rows:
        acct = float(r.get("accountValue") or 0)
        day, week, month, all_ = (_window(r, w) for w in ("day", "week", "month", "allTime"))
        if acct < SNAPSHOT_MIN_ACCOUNT or (_f(month, "vlm") or 0) < SNAPSHOT_MIN_MONTH_VLM:
            continue
        out.append((today, r["ethAddress"].lower(), acct,
                    _f(day, "pnl"), _f(week, "pnl"), _f(month, "pnl"), _f(all_, "pnl"),
                    _f(month, "roi"), _f(all_, "roi"),
                    _f(day, "vlm"), _f(month, "vlm"), _f(all_, "vlm")))
    conn.executemany(
        """
        INSERT INTO hl_leaderboard_snapshots
            (snapshot_date, address, account_value, pnl_day, pnl_week, pnl_month, pnl_all,
             roi_month, roi_all, vlm_day, vlm_month, vlm_all)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_date, address) DO NOTHING
        """,
        out,
    )
    conn.commit()
    return rows, len(out)


def add_tracked_wallets(conn, rows, today):
    """Adds up to NEW_WALLETS_PER_DAY random eligible wallets, until
    MAX_TRACKED. Seeded by date, so a re-run the same day picks the same
    ones. Returns how many were added."""
    tracked = {r["address"] for r in conn.execute("SELECT address FROM hl_tracked_wallets").fetchall()}
    room = min(NEW_WALLETS_PER_DAY, MAX_TRACKED - len(tracked))
    if room <= 0:
        return 0
    eligible = sorted(
        r["ethAddress"].lower() for r in rows
        if float(r.get("accountValue") or 0) >= TRACK_MIN_ACCOUNT
        and (_f(_window(r, "month"), "vlm") or 0) >= TRACK_MIN_MONTH_VLM
        and r["ethAddress"].lower() not in tracked
    )
    picked = random.Random(today).sample(eligible, min(room, len(eligible)))
    for a in picked:
        conn.execute(
            "INSERT INTO hl_tracked_wallets (address, added_date, status) VALUES (?, ?, 'tracking') ON CONFLICT (address) DO NOTHING",
            (a, today),
        )
    conn.commit()
    return len(picked)


def _fetch_fills(address, start_ms, limit=None):
    """Fills from start_ms forward, paging until caught up, or None once
    more than `limit` have come back (too active to store)."""
    fills = []
    while True:
        batch = _info({"type": "userFillsByTime", "user": address, "startTime": start_ms})
        if not isinstance(batch, list) or not batch:
            return fills
        fills += batch
        if limit is not None and len(fills) > limit:
            return None
        if len(batch) < PAGE:
            return fills
        start_ms = max(f["time"] for f in batch) + 1
        time.sleep(0.3)


def record_fills(conn, now_ms):
    """Fetches new fills for every 'tracking' wallet. Returns (wallets
    updated, fills stored, wallets newly marked too_active)."""
    wallets = conn.execute("SELECT address, last_fill_ts FROM hl_tracked_wallets WHERE status = 'tracking'").fetchall()
    updated = stored = too_active = 0
    for w in wallets:
        first = w["last_fill_ts"] is None
        start = now_ms - FIRST_FETCH_DAYS * _DAY_MS if first else w["last_fill_ts"] + 1
        try:
            fills = _fetch_fills(w["address"], start, TOO_ACTIVE_FILLS if first else None)
        except Exception as e:
            print(f"fill fetch failed for one wallet, retried next run: {type(e).__name__}")
            continue
        if fills is None:
            conn.execute("UPDATE hl_tracked_wallets SET status = 'too_active' WHERE address = ?", (w["address"],))
            too_active += 1
            continue
        rows = [(w["address"], int(f["tid"]), f["coin"], f["side"], float(f["px"]), float(f["sz"]), int(f["time"]),
                 f.get("dir"), float(f["startPosition"]) if f.get("startPosition") not in (None, "") else None,
                 float(f["closedPnl"]) if f.get("closedPnl") not in (None, "") else None,
                 float(f["fee"]) if f.get("fee") not in (None, "") else None, f.get("feeToken"),
                 bool(f.get("crossed")), int(f["oid"]) if f.get("oid") is not None else None) for f in fills]
        if rows:
            conn.executemany(
                """
                INSERT INTO hl_fills (address, tid, coin, side, px, sz, time, dir, start_position, closed_pnl, fee, fee_token, crossed, oid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (address, tid) DO NOTHING
                """,
                rows,
            )
        last = max((r[6] for r in rows), default=w["last_fill_ts"] or start - 1)
        conn.execute(
            "UPDATE hl_tracked_wallets SET last_fill_ts = ?, fills_stored = fills_stored + ? WHERE address = ?",
            (last, len(rows), w["address"]),
        )
        conn.commit()
        updated += 1
        stored += len(rows)
        time.sleep(0.3)
    conn.commit()
    return updated, stored, too_active


def run():
    conn = get_live_conn()
    now_ms = int(time.time() * 1000)
    today = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
    rows, n_snap = snapshot_leaderboard(conn, today)
    n_new = add_tracked_wallets(conn, rows, today)
    n_upd, n_fills, n_hft = record_fills(conn, now_ms)
    n_tracking = conn.execute("SELECT COUNT(*) AS n FROM hl_tracked_wallets WHERE status = 'tracking'").fetchone()["n"]
    # Daily paper run of the perp strategy (scripts/hl_paper.py) -- after
    # the recorder's own commits, so a failure here can't cost them.
    try:
        n_paper = hl_paper.run_daily(conn)
        conn.commit()
    except Exception as e:
        n_paper = 0
        print(f"hl paper run failed this run: {type(e).__name__}")
    print(f"leaderboard: {len(rows)} accounts, {n_snap} stored | tracked wallets: +{n_new} added, {n_tracking} tracking, "
          f"{n_hft} newly marked too active | fills: {n_fills} stored across {n_upd} wallet(s) | paper strategy: {n_paper} day(s) recorded")
    conn.close()


if __name__ == "__main__":
    run()
