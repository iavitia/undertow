import json
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from analysis.pnl import intrinsic_value, nearest_index_price
from clients.blockscout_client import iter_token_transfers
from clients.factbase_client import get_posts_for_window
from config import DB_PATH, HYPOTHETICAL_STAKE_USD, HYPOTHETICAL_STAKE_USD_SMALL

app = FastAPI(title="undertow")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ASSET_NAMES = {"BTC": "bitcoin", "ETH": "ethereum"}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # scripts/run_watchlist_agent.py now writes to this same DB on an
    # unattended 15-min schedule -- without this, a request landing mid-write
    # gets "database is locked" immediately instead of waiting briefly for
    # the other transaction to finish (confirmed crashing a request this
    # session). 8s is comfortably longer than any single write in that
    # agent's per-tick work.
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


def day_str(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def magnitude_from_score(zscore, cap=6):
    if zscore is None:
        return None
    return round(max(0, min(zscore, cap)) / cap * 100)


def event_magnitude(row):
    """Whichever detector fired, on its own scale: anomaly_score is a
    z-score (cap 6), wallet_jump_ratio is a multiplier (cap 20x, log-scaled
    since jump ratios span a much wider range than z-scores). Shows the
    more extreme of the two if both fired."""
    import math

    scores = []
    if row["anomaly_score"] is not None:
        scores.append(magnitude_from_score(row["anomaly_score"], cap=6))
    if row["wallet_jump_ratio"] is not None:
        scores.append(magnitude_from_score(math.log(max(row["wallet_jump_ratio"], 1)), cap=math.log(20)))
    return max(scores) if scores else None


def event_to_dict(row):
    keys = row.keys()
    return {
        "id": row["id"],
        "asset": row["asset"],
        "instrument": row["instrument"],
        "strike": row["strike"],
        "option_type": row["option_type"],
        "side": row["side"],
        "size": row["size"],
        "price": row["price"],
        "notional_usd": row["notional_usd"],
        "index_price_usd": row["index_price_usd"],
        "wallet_address": row["wallet_address"],
        "wallet_alias": row["wallet_alias"] if "wallet_alias" in keys else None,
        "tx_hash": row["tx_hash"],
        "source_trade_id": row["source_trade_id"],
        "timestamp": row["timestamp"],
        "date": day_str(row["timestamp"]),
        "expiry": row["expiry"],
        "expiry_date": datetime.fromtimestamp(row["expiry"], tz=timezone.utc).strftime("%Y-%m-%d"),
        "flag_reasons": row["flag_reason"].split(",") if row["flag_reason"] else [],
        "anomaly_score": row["anomaly_score"],
        "wallet_jump_ratio": row["wallet_jump_ratio"],
        "magnitude": event_magnitude(row),
        "mark_price": row["mark_price"] if "mark_price" in keys else None,
        "rfq_id": row["rfq_id"] if "rfq_id" in keys else None,
        "tx_status": row["tx_status"] if "tx_status" in keys else None,
        "trade_fee": row["trade_fee"] if "trade_fee" in keys else None,
        "liquidity_role": row["liquidity_role"] if "liquidity_role" in keys else None,
        "realized_pnl": row["realized_pnl"] if "realized_pnl" in keys else None,
    }


def find_position_lifecycle(conn, wallet_address, instrument, asset, entry_ts, entry_side, expiry_ts):
    """Best-effort lifecycle for a single opened position: did the wallet
    close it (opposite side, same instrument, later), roll it (closed, then
    opened a different instrument on the same asset within 2h), or is it
    still open/expired per our data? Not authoritative -- this is inferred
    from trade sequence, not on-chain settlement records."""
    opposite = "sell" if entry_side == "buy" else "buy"
    close_row = conn.execute(
        """
        SELECT timestamp FROM options_events
        WHERE wallet_address = ? AND instrument = ? AND side = ? AND timestamp > ?
        ORDER BY timestamp ASC LIMIT 1
        """,
        (wallet_address, instrument, opposite, entry_ts),
    ).fetchone()

    if close_row is None:
        now_ms = int(time.time() * 1000)
        status = "expired" if now_ms > expiry_ts * 1000 else "open"
        return {"status": status, "close_ts": None, "roll_to_instrument": None}

    close_ts = close_row["timestamp"]
    roll_row = conn.execute(
        """
        SELECT instrument FROM options_events
        WHERE wallet_address = ? AND asset = ? AND instrument != ?
          AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp ASC LIMIT 1
        """,
        (wallet_address, asset, instrument, close_ts, close_ts + 2 * 3600 * 1000),
    ).fetchone()

    if roll_row:
        return {"status": "rolled", "close_ts": close_ts, "roll_to_instrument": roll_row["instrument"]}
    return {"status": "closed", "close_ts": close_ts, "roll_to_instrument": None}


CATALYSTS = [
    {
        "id": "reserve",
        "label": "Strategic BTC Reserve announcement",
        "asset": "BTC",
        "claim": "CoinDesk: Trump announced via Truth Social the US would strategically hold Bitcoin; BTC +8.2% within 24h. Cited date: 2025-03-03.",
        "confirmed_move_utc": "2025-03-02T15:55:00Z",
        "confirmed_note": (
            "Sharp rise 2025-03-02 ~15:55-18:00 UTC, $87,398 -> $94,957 (+8.6%), matching magnitude "
            "closely but one calendar day earlier than the cited date (likely a US-Eastern vs UTC "
            "date-labeling difference). The gain fully reversed the next day: March 2 close +9.5%, "
            "March 3 close -8.6% -- a spike-and-fade, not a sustained move."
        ),
        "verdict": "no_signal",
        "verdict_note": (
            "The one large trade in the 3-day run-up (faded-vault-b6 buying $8.18M of ETH calls, "
            "flagged large_trade) turned out to be part of a longstanding weekly systematic "
            "call-buying pattern -- 43 trades, same instrument shape, same time of day "
            "(~13:0X UTC), roughly weekly from Jan-Aug 2025 regardless of news. Coincidental timing, "
            "not evidence of pre-positioning. No other standout activity found in the run-up."
        ),
        "related_event_ids": [984542, 984544, 984546],
        "related_wallet_aliases": ["faded-vault-b6"],
    },
    {
        "id": "tariff_crash",
        "label": "China tariff announcement crash",
        "asset": "BTC",
        "claim": (
            "CoinDesk: Trump announced a 100% tariff on Chinese imports via Truth Social in response "
            "to China's rare-earth export controls; BTC fell 12.4% within 2 hours, $19.38B sell-off "
            "in 24h, the largest single-day loss in Bitcoin's history. Cited date: 2025-10-10."
        ),
        "confirmed_move_utc": "2025-10-10T21:19:00Z",
        "confirmed_note": (
            "Cleanest match of the four: same date as cited. Crash pinpointed to 21:19-21:21 UTC, "
            "-8.25% in 10 min / -10.22% in 30 min ($117,112 -> $106,131), partial recovery to "
            "~$113,700 within the hour. Day-over-day close -7.03%."
        ),
        "verdict": "strong_signal",
        "verdict_note": (
            "husk-ridge-5f went dormant for over 3 months (last trade 2025-07-04) then reappeared "
            "2025-10-10 16:16 UTC -- about 5 hours before the crash -- buying $546K of deep-OTM BTC "
            "downside puts (100,000 strike, ~15% OTM), their single largest trade ever against a "
            "$56K median. Caught by the wallet_size_jump detector. Correlation, not proof of "
            "foreknowledge -- but exactly the behavioral pattern this project is built to surface."
        ),
        "related_event_ids": [1175540],
        "related_wallet_aliases": ["husk-ridge-5f"],
    },
    {
        "id": "genius_act",
        "label": "GENIUS/CLARITY Act comments",
        "asset": "BTC",
        "claim": (
            "CoinDesk: Trump accused Wall Street banks of undermining the GENIUS Act and delaying "
            "the CLARITY Act via Truth Social; BTC +5.2% within 10 minutes. Cited date: 2026-03-03."
        ),
        "confirmed_move_utc": "2026-03-02T14:47:00Z",
        "confirmed_note": (
            "Gradual rise 2026-03-02 14:47-16:49 UTC, $66,709 -> $69,816 (+4.6% over ~2h) -- one "
            "calendar day earlier than cited, and a grind rather than a sharp 10-minute spike in our "
            "data. Day-over-day close +4.74%."
        ),
        "verdict": "mixed_signal",
        "verdict_note": (
            "Ambiguous. cedar-thicket-61 (wallet_size_jump) bought a BTC call early on Feb 28 then "
            "sold a similar one later the same day -- unclear net position by the time of the actual "
            "move. cedar-basin-72 (wallet_size_jump) sold a call, the wrong-direction bet if this "
            "were informed positioning. The known OTC desk pair ran a $16.2M bullish call spread on "
            "Feb 28, but they trade constantly in both directions, so it's hard to read as "
            "meaningfully tied to this event specifically rather than routine flow."
        ),
        "related_event_ids": [1123816, 1124222, 1124278, 1123846, 1123847, 1123848, 1123849],
        "related_wallet_aliases": ["cedar-thicket-61", "cedar-basin-72", "grey-delta-21", "umber-signal-67"],
    },
    {
        "id": "iran_talks",
        "label": "Iran peace talks announcement",
        "asset": "BTC",
        "claim": (
            "CoinDesk: following a naval blockade of the Strait of Hormuz, Trump announced Iran had "
            "reached out for peace talks with a high likelihood of a deal; BTC +6.2% within 30 "
            "minutes. Cited date: 2026-04-14."
        ),
        "confirmed_move_utc": "2026-04-13T21:40:00Z",
        "confirmed_note": (
            "Weakest match of the four. Real and directionally right -- day-over-day close +5.49% "
            "on April 13, one day earlier than cited -- but accrued gradually across the evening "
            "(21:40-23:26 UTC, +2.0%) rather than in a sharp 30-minute burst as described."
        ),
        "verdict": "no_signal",
        "verdict_note": "No flagged events and no standout unflagged trades found anywhere in the 3-day run-up. Null result on both the price-confirmation and the options-activity side.",
        "related_event_ids": [],
        "related_wallet_aliases": [],
    },
]


@app.get("/api/catalysts")
def list_catalysts():
    return [{k: v for k, v in c.items() if k != "related_event_ids"} | {"related_event_ids": c["related_event_ids"]} for c in CATALYSTS]


@app.get("/api/catalysts/{catalyst_id}")
def catalyst_detail(catalyst_id: str):
    catalyst = next((c for c in CATALYSTS if c["id"] == catalyst_id), None)
    if catalyst is None:
        raise HTTPException(404, "catalyst not found")

    conn = get_conn()
    events_by_id = {}
    positions = []
    if catalyst["related_event_ids"]:
        placeholders = ",".join("?" * len(catalyst["related_event_ids"]))
        rows = conn.execute(
            EVENTS_SELECT + f" WHERE oe.id IN ({placeholders}) ORDER BY oe.timestamp", catalyst["related_event_ids"]
        ).fetchall()
        for r in rows:
            events_by_id[r["id"]] = event_to_dict(r)

        # group legs into distinct positions (same wallet + same instrument =
        # one opened position, however many fills it took) for chart markers
        earliest_by_key = {}
        for ev in events_by_id.values():
            key = (ev["wallet_address"], ev["instrument"])
            if key not in earliest_by_key or ev["timestamp"] < earliest_by_key[key]["timestamp"]:
                earliest_by_key[key] = ev
        for (wallet_address, instrument), ev in earliest_by_key.items():
            lifecycle = find_position_lifecycle(
                conn, wallet_address, instrument, ev["asset"], ev["timestamp"], ev["side"], ev["expiry"]
            )
            positions.append({
                "wallet_alias": ev["wallet_alias"],
                "instrument": instrument,
                "asset": ev["asset"],
                "entry_ts": ev["timestamp"],
                "side": ev["side"],
                "expiry": ev["expiry"],
                **lifecycle,
            })

            # the closing trade is often not in the manually-curated
            # related_event_ids (those were picked to illustrate the
            # opening position) -- pull it in automatically so "related
            # transactions" doesn't silently show the open with no close
            if lifecycle["close_ts"] is not None:
                close_row = conn.execute(
                    EVENTS_SELECT + " WHERE oe.wallet_address = ? AND oe.instrument = ? AND oe.timestamp = ? LIMIT 1",
                    (wallet_address, instrument, lifecycle["close_ts"]),
                ).fetchone()
                if close_row is not None:
                    events_by_id[close_row["id"]] = event_to_dict(close_row)
    conn.close()

    events = sorted(events_by_id.values(), key=lambda e: e["timestamp"])

    move_dt = datetime.strptime(catalyst["confirmed_move_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return {**catalyst, "events": events, "positions": positions, "confirmed_move_date": move_dt.strftime("%Y-%m-%d")}


# Note: queries below deliberately don't filter on source = 'derive' even
# though the column exists -- every row is 'derive' now that Deribit was
# dropped, so the predicate is vacuous, and worse, it actively misled the
# query planner away from the selective partial index on is_flagged
# (confirmed: EXPLAIN QUERY PLAN picked the source-matching unique index
# over idx_options_events_flagged, turning a 0.1s query into an 8-11s one).
EVENTS_SELECT = """
    SELECT oe.*, w.alias AS wallet_alias
    FROM options_events oe
    LEFT JOIN wallets w ON w.address = oe.wallet_address
"""


@app.get("/api/meta")
def meta():
    conn = get_conn()
    row = conn.execute("SELECT MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts FROM options_events").fetchone()
    conn.close()
    return {
        "assets": ["ETH", "BTC"],
        "date_range": {"start": day_str(row["min_ts"]), "end": day_str(row["max_ts"])},
        "generated_at": int(time.time() * 1000),
    }


@app.get("/api/pipeline-status")
def pipeline_status():
    """Is the live data pipeline actually running, not just "should be
    running" -- scripts/run_watchlist_agent.py's own tick history
    (agent_runs), plus how stale the last successful ingest is."""
    conn = get_conn()
    ingest_row = conn.execute("SELECT last_ts FROM ingest_state WHERE source = 'derive_live'").fetchone()
    now_ms = int(time.time() * 1000)
    total_events = conn.execute("SELECT COUNT(*) FROM options_events").fetchone()[0]
    total_wallets = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    recent_runs = conn.execute("SELECT * FROM agent_runs ORDER BY run_ts DESC LIMIT 20").fetchall()
    conn.close()
    return {
        "last_ingest_ts": ingest_row["last_ts"] if ingest_row else None,
        "minutes_since_ingest": round((now_ms - ingest_row["last_ts"]) / 60000, 1) if ingest_row else None,
        "total_events": total_events,
        "total_wallets": total_wallets,
        "recent_runs": [
            {
                "run_ts": r["run_ts"],
                "ingested_count": r["ingested_count"],
                "opened_count": r["opened_count"],
                "advanced_count": r["advanced_count"],
                "resolved_count": r["resolved_count"],
                "skipped_naked_count": r["skipped_naked_count"],
                "skipped_weak_edge_count": r["skipped_weak_edge_count"],
                "stopped_out_count": r["stopped_out_count"],
            }
            for r in recent_runs
        ],
    }


@app.get("/api/recent-trades")
def recent_trades(limit: int = 50, offset: int = 0):
    """Every trade as it lands in the DB (market-wide, not just watchlist
    wallets), with whatever scoring we currently have for that wallet
    layered on -- most wallets won't have all of it (see conversation: full
    copy-backtest Wilson covers 7,436 wallets, the per-bucket "trade-level
    confidence" only 3,025, walk-forward-validated Wilson only 375). Sorted
    by insertion order (oe.id), not trade timestamp -- id reflects when
    something actually became visible to us, which is the honest
    definition of "new" for a live feed, and is already the primary key so
    this needs no extra index on a 700k+ row table."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT oe.id, oe.instrument, oe.asset, oe.option_type, oe.strike, oe.expiry, oe.side, oe.size, oe.price,
               oe.notional_usd, oe.timestamp, oe.wallet_address, oe.rfq_id,
               w.alias,
               ctb.copy_positions, ctb.copy_win_rate_wilson_low, ctb.copy_total_return_pct,
               wep.n AS edge_n, wep.wilson_low AS edge_wilson_low, wep.median_return_pct AS edge_median_return_pct,
               wf.formation_wilson_low, wf.validation_wilson_low,
               cc.passes_screen,
               (wl.wallet_address IS NOT NULL AND wl.is_active = 1) AS on_watchlist
        FROM options_events oe
        LEFT JOIN wallets w ON w.address = oe.wallet_address
        LEFT JOIN copy_trade_backtest ctb ON ctb.wallet_address = oe.wallet_address
        LEFT JOIN wallet_edge_profile wep ON wep.wallet_address = oe.wallet_address
            AND wep.asset = oe.asset AND wep.option_type = oe.option_type AND wep.entry_side = oe.side
        LEFT JOIN walkforward_backtest wf ON wf.wallet_address = oe.wallet_address
        LEFT JOIN copy_candidates cc ON cc.wallet_address = oe.wallet_address
        LEFT JOIN watchlist wl ON wl.wallet_address = oe.wallet_address
        WHERE oe.wallet_address IS NOT NULL
        ORDER BY oe.id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "instrument": r["instrument"],
            "asset": r["asset"],
            "option_type": r["option_type"],
            "strike": r["strike"],
            "expiry_date": datetime.fromtimestamp(r["expiry"], tz=timezone.utc).strftime("%Y-%m-%d"),
            "side": r["side"],
            "size": r["size"],
            "price": r["price"],
            "notional_usd": r["notional_usd"],
            "timestamp": r["timestamp"],
            "wallet_address": r["wallet_address"],
            "alias": r["alias"],
            "is_rfq": r["rfq_id"] is not None,
            "on_watchlist": bool(r["on_watchlist"]),
            "copy_positions": r["copy_positions"],
            "copy_win_rate_wilson_low": round(r["copy_win_rate_wilson_low"] * 100, 1) if r["copy_win_rate_wilson_low"] is not None else None,
            "copy_total_return_pct": round(r["copy_total_return_pct"] * 100, 1) if r["copy_total_return_pct"] is not None else None,
            "edge_n": r["edge_n"],
            "edge_wilson_low": round(r["edge_wilson_low"] * 100, 1) if r["edge_wilson_low"] is not None else None,
            "edge_median_return_pct": round(r["edge_median_return_pct"] * 100, 1) if r["edge_median_return_pct"] is not None else None,
            "formation_wilson_low": round(r["formation_wilson_low"] * 100, 1) if r["formation_wilson_low"] is not None else None,
            "validation_wilson_low": round(r["validation_wilson_low"] * 100, 1) if r["validation_wilson_low"] is not None else None,
            "passes_screen": bool(r["passes_screen"]) if r["passes_screen"] is not None else None,
        }
        for r in rows
    ]


@app.get("/api/events")
def list_events(asset: str | None = None, flag_reason: str | None = None, limit: int = 200):
    conn = get_conn()
    query = EVENTS_SELECT + " WHERE oe.is_flagged = 1"
    params = []
    if asset:
        query += " AND oe.asset = ?"
        params.append(asset.upper())
    if flag_reason:
        query += " AND oe.flag_reason LIKE ?"
        params.append(f"%{flag_reason}%")
    query += " ORDER BY oe.timestamp DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [event_to_dict(r) for r in rows]


@app.get("/api/events/{event_id}")
def get_event(event_id: int):
    conn = get_conn()
    row = conn.execute(EVENTS_SELECT + " WHERE oe.id = ?", (event_id,)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, "event not found")

    d = event_to_dict(row)
    lifecycle = find_position_lifecycle(
        conn, row["wallet_address"], row["instrument"], row["asset"], row["timestamp"], row["side"], row["expiry"]
    )
    raw = json.loads(row["raw_json"]) if row["raw_json"] else None
    conn.close()
    return {**d, **lifecycle, "raw": raw}


@app.get("/api/daily_signals")
def daily_signals(asset: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM daily_signals WHERE asset = ? ORDER BY day", (asset.upper(),)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/prices")
def prices(asset: str, granularity: str = "daily", from_ts: int | None = None, to_ts: int | None = None):
    """Price series derived from index_price_usd recorded on every options
    trade -- there is no separate price backfill; CoinGecko's free tier
    only allows querying the last 365 days of history, which doesn't reach
    back to the backfill start date, so this reuses data we already have."""
    if granularity not in ("daily", "hourly"):
        raise HTTPException(400, "granularity must be 'daily' or 'hourly'")
    bucket_fmt = "%Y-%m-%d" if granularity == "daily" else "%Y-%m-%dT%H:00:00"

    conn = get_conn()
    query = f"""
        SELECT bucket, index_price_usd, timestamp FROM (
            SELECT
                strftime('{bucket_fmt}', timestamp / 1000, 'unixepoch') AS bucket,
                index_price_usd,
                timestamp,
                ROW_NUMBER() OVER (
                    PARTITION BY strftime('{bucket_fmt}', timestamp / 1000, 'unixepoch')
                    ORDER BY timestamp DESC
                ) AS rn
            FROM options_events
            WHERE asset = ?
    """
    params = [asset.upper()]
    if from_ts is not None:
        query += " AND timestamp >= ?"
        params.append(from_ts)
    if to_ts is not None:
        query += " AND timestamp <= ?"
        params.append(to_ts)
    query += ") WHERE rn = 1 ORDER BY bucket"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [{"bucket": r["bucket"], "timestamp": r["timestamp"], "price": r["index_price_usd"]} for r in rows]


WALLET_STATS_QUERY = """
    SELECT
        oe.wallet_address,
        w.alias AS wallet_alias,
        COUNT(*) AS flagged_event_count,
        SUM(oe.notional_usd) AS total_notional_usd,
        AVG(oe.anomaly_score) AS avg_anomaly_score,
        MIN(oe.timestamp) AS first_flagged_ts,
        MAX(oe.timestamp) AS last_flagged_ts,
        GROUP_CONCAT(DISTINCT oe.asset) AS assets,
        SUM(CASE WHEN oe.flag_reason LIKE '%large_trade%' THEN 1 ELSE 0 END) AS large_trade_count,
        SUM(CASE WHEN oe.flag_reason LIKE '%wallet_size_jump%' THEN 1 ELSE 0 END) AS wallet_jump_count
    FROM options_events oe
    LEFT JOIN wallets w ON w.address = oe.wallet_address
    WHERE oe.is_flagged = 1 AND oe.wallet_address = ?
"""


def compute_wallet_positions(conn, address):
    """Per-instrument position P&L for one wallet -- shared by /portfolio
    and /value-timeline so they can't drift out of sync (see conversation:
    value-timeline used to sum raw realized_pnl directly and so missed the
    expiry-settlement estimate entirely, showing a straight-down fee bleed
    for wallets like brass-basin-71 despite a 92%+ win rate once expiry
    settlement is accounted for). See wallet_portfolio's docstring for the
    settlement-estimate reasoning."""
    rows = conn.execute(
        """
        SELECT instrument, asset, option_type, strike, expiry, side, size, price, timestamp,
               realized_pnl, notional_usd, trade_fee
        FROM options_events
        WHERE wallet_address = ?
        ORDER BY instrument, timestamp
        """,
        (address,),
    ).fetchall()

    grouped = {}
    for r in rows:
        grouped.setdefault(r["instrument"], []).append(r)

    now_ms = int(time.time() * 1000)
    positions = []
    for instrument, legs in grouped.items():
        first, last = legs[0], legs[-1]
        total_fees = sum(l["trade_fee"] or 0 for l in legs)
        net_qty = sum((l["size"] or 0) if l["side"] == "buy" else -(l["size"] or 0) for l in legs)
        cashflow = sum((l["price"] or 0) * (l["size"] or 0) * (1 if l["side"] == "sell" else -1) for l in legs)
        expiry_ms = first["expiry"] * 1000
        expired = expiry_ms <= now_ms

        pnl_is_estimated = False
        if expired:
            settlement_cashflow = 0.0
            if abs(net_qty) > 1e-9:
                spot, _gap_ms = nearest_index_price(conn, first["asset"], expiry_ms)
                if spot is not None:
                    settlement_cashflow = net_qty * intrinsic_value(spot, first["strike"], first["option_type"])
                    pnl_is_estimated = True
            net_pnl = cashflow + settlement_cashflow - total_fees
            is_resolved = True
        else:
            net_pnl = sum(l["realized_pnl"] or 0 for l in legs)
            is_resolved = abs(net_pnl) > 1e-6

        positions.append({
            "instrument": instrument,
            "asset": first["asset"],
            "option_type": first["option_type"],
            "strike": first["strike"],
            "expiry": first["expiry"],
            "entry_ts": first["timestamp"],
            "last_ts": last["timestamp"],
            # when this position's P&L became knowable: the last real trade
            # for a position closed by an offsetting trade, or expiry for
            # one settled by our own estimate -- used to bucket value-timeline.
            "resolved_ts": expiry_ms if (expired and pnl_is_estimated) else last["timestamp"],
            "entry_side": first["side"],
            "leg_count": len(legs),
            "net_pnl": round(net_pnl, 2),
            "avg_notional": round(sum(l["notional_usd"] or 0 for l in legs) / len(legs), 2),
            "total_fees": round(total_fees, 2),
            "entry_date": day_str(first["timestamp"]),
            "last_activity_date": day_str(last["timestamp"]),
            "expiry_date": datetime.fromtimestamp(first["expiry"], tz=timezone.utc).strftime("%Y-%m-%d"),
            "is_resolved": is_resolved,
            "pnl_is_estimated": pnl_is_estimated,
        })
    positions.sort(key=lambda p: p["entry_ts"])
    return positions


def get_mm_map(conn, addresses):
    """Look up the market-maker heuristic (see scripts/build_leaderboard.py)
    for a batch of addresses at once. Kept as a side lookup rather than a
    JOIN in WALLET_STATS_QUERY because that query's WHERE is_flagged=1
    filter means a wallet with zero flagged events would join to nothing
    and lose the flag entirely."""
    if not addresses:
        return {}
    placeholders = ",".join("?" * len(addresses))
    rows = conn.execute(
        f"SELECT wallet_address, is_likely_bot, legs_per_position, legs_per_day "
        f"FROM wallet_leaderboard WHERE wallet_address IN ({placeholders})",
        addresses,
    ).fetchall()
    return {r["wallet_address"]: r for r in rows}


def wallet_row_to_dict(r, featured=False, mm=None):
    return {
        "address": r["wallet_address"],
        "alias": r["wallet_alias"],
        "flagged_event_count": r["flagged_event_count"] or 0,
        "total_notional_usd": r["total_notional_usd"],
        "avg_magnitude": magnitude_from_score(r["avg_anomaly_score"]),
        "first_flagged_date": day_str(r["first_flagged_ts"]) if r["first_flagged_ts"] else None,
        "last_flagged_date": day_str(r["last_flagged_ts"]) if r["last_flagged_ts"] else None,
        "assets": r["assets"].split(",") if r["assets"] else [],
        "large_trade_count": r["large_trade_count"] or 0,
        "wallet_jump_count": r["wallet_jump_count"] or 0,
        "featured": featured,
        "is_likely_market_maker": bool(mm["is_likely_bot"]) if mm else False,
        "legs_per_position": round(mm["legs_per_position"], 1) if mm else None,
        "legs_per_day": round(mm["legs_per_day"], 1) if mm else None,
    }


@app.get("/api/leaderboard")
def leaderboard(sort: str = "pnl", min_resolved: int = 5, include_bots: bool = False, limit: int = 25):
    """Ranked wallets by realized P&L, built from wallet_leaderboard (see
    scripts/build_leaderboard.py -- rebuilt after backfills, not computed
    live: the underlying aggregation over 728k+ rows takes ~5-6s, too slow
    for a per-request call). min_resolved filters out wallets whose
    ranking would be dominated by a tiny, luck-prone sample (e.g. 100% win
    rate on 2 trades). is_likely_bot separates automated market-making
    (thousands of small fills) from discretionary directional trading --
    excluded by default since "who made the most money" is much more
    interesting for a wallet running a handful of deliberate positions
    than for a bot whose P&L just scales with volume."""
    if sort not in ("pnl", "win_rate"):
        raise HTTPException(400, "sort must be 'pnl' or 'win_rate'")

    conn = get_conn()
    query = """
        SELECT wl.*, w.alias FROM wallet_leaderboard wl
        LEFT JOIN wallets w ON w.address = wl.wallet_address
        WHERE wl.resolved >= ?
    """
    params = [min_resolved]
    if not include_bots:
        query += " AND wl.is_likely_bot = 0"
    order_col = "wl.net_of_fees" if sort == "pnl" else "wl.win_rate"
    query += f" ORDER BY {order_col} DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [
        {
            "address": r["wallet_address"],
            "alias": r["alias"],
            "positions": r["positions"],
            "resolved": r["resolved"],
            "wins": r["wins"],
            "losses": r["losses"],
            "win_rate": round(r["win_rate"] * 100, 1) if r["win_rate"] is not None else None,
            "total_pnl": round(r["total_pnl"], 2),
            "total_fees": round(r["total_fees"], 2),
            "net_of_fees": round(r["net_of_fees"], 2),
            "legs_per_position": round(r["legs_per_position"], 1),
            "legs_per_day": round(r["legs_per_day"], 1),
            "active_days": r["active_days"],
            "is_likely_bot": bool(r["is_likely_bot"]),
            "call_pct": round(r["call_pct"] * 100, 1),
            "buy_pct": round(r["buy_pct"] * 100, 1),
            "btc_pct": round(r["btc_pct"] * 100, 1),
            "avg_notional_usd": round(r["avg_notional_usd"], 2),
            "first_date": day_str(r["first_ts"]),
            "last_date": day_str(r["last_ts"]),
        }
        for r in rows
    ]


@app.get("/api/copy-backtest")
def copy_backtest(sort: str = "wilson", min_positions: int = 20, include_bots: bool = False, limit: int = 30):
    """Ranked by scripts/backtest_copy_trades.py: for every position a
    wallet opened, would a copier -- entering at the next print by another
    wallet in that instrument, not this wallet's own fill -- have profited?
    A different question than /api/leaderboard's "was this wallet
    profitable," and the two rankings diverge a lot: sorting by win_rate
    surfaces a lot of naked-premium sellers with a high hit rate and
    catastrophic tail risk (many small wins wiped out by one bad print --
    see conversation, ~1 in 4 wallets with a 70%+ win rate and 20+ sampled
    positions are net-negative overall). copy_win_rate_wilson_low (95%
    Wilson lower bound, not raw win_rate) is the default sort for exactly
    that reason -- it discounts small samples instead of trusting them.
    avg/total return-on-premium can blow up on cheap deep-OTM options (a
    tiny percent-of-premium denominator), so median_return_pct is the more
    robust read on typical outcome; avg/total are still shown since large
    wins/losses are real information, just noisier."""
    if sort not in ("wilson", "win_rate", "avg_return", "median_return", "total_return"):
        raise HTTPException(400, "sort must be one of: wilson, win_rate, median_return, avg_return, total_return")

    conn = get_conn()
    query = """
        SELECT c.*, w.alias, l.is_likely_bot FROM copy_trade_backtest c
        LEFT JOIN wallets w ON w.address = c.wallet_address
        LEFT JOIN wallet_leaderboard l ON l.wallet_address = c.wallet_address
        WHERE c.copy_positions >= ?
    """
    params = [min_positions]
    if not include_bots:
        query += " AND (l.is_likely_bot IS NULL OR l.is_likely_bot = 0)"
    order_col = {
        "wilson": "c.copy_win_rate_wilson_low",
        "win_rate": "c.copy_win_rate",
        "avg_return": "c.copy_avg_return_pct",
        "median_return": "c.copy_median_return_pct",
        "total_return": "c.copy_total_return_pct",
    }[sort]
    query += f" ORDER BY {order_col} DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [
        {
            "address": r["wallet_address"],
            "alias": r["alias"],
            "copy_positions": r["copy_positions"],
            "copy_wins": r["copy_wins"],
            "copy_losses": r["copy_losses"],
            "copy_win_rate": round(r["copy_win_rate"] * 100, 1) if r["copy_win_rate"] is not None else None,
            "copy_win_rate_wilson_low": round(r["copy_win_rate_wilson_low"] * 100, 1) if r["copy_win_rate_wilson_low"] is not None else None,
            "copy_avg_return_pct": round(r["copy_avg_return_pct"] * 100, 1),
            "copy_median_return_pct": round(r["copy_median_return_pct"] * 100, 1),
            "copy_total_return_pct": round(r["copy_total_return_pct"] * 100, 1),
            "excluded_stale": r["excluded_stale"],
            "excluded_open": r["excluded_open"],
            "is_likely_bot": bool(r["is_likely_bot"]) if r["is_likely_bot"] is not None else False,
        }
        for r in rows
    ]


@app.get("/api/wallet-qualification")
def wallet_qualification(tier: str | None = None, sort: str = "forward_wilson_low", limit: int = 60):
    """scripts/build_wallet_qualification.py / analysis/qualification.py:
    the no-look-ahead qualification walk that replaced the old fixed-split
    walk-forward + composite screen (see conversation -- that design
    couldn't evaluate any wallet whose whole history started after one
    global cutoff date, and didn't check whether a wallet would have
    actually been selected at the time it supposedly showed persistence).
    tier is one of insufficient_data / does_not_qualify / qualified_but_faded
    / provisional / validated / high_confidence -- see analysis/qualification.py's
    docstring for what each means. forward_* fields are the genuine
    no-look-ahead record: performance strictly after the wallet first
    cleared the qualification bar, using only data available at that
    point."""
    valid_sorts = ("forward_wilson_low", "forward_total_return_pct", "total_n", "risk_discipline_score")
    if sort not in valid_sorts:
        raise HTTPException(400, f"sort must be one of: {', '.join(valid_sorts)}")
    valid_tiers = ("insufficient_data", "does_not_qualify", "qualified_but_faded", "provisional", "validated", "high_confidence")
    if tier is not None and tier not in valid_tiers:
        raise HTTPException(400, f"tier must be one of: {', '.join(valid_tiers)}")

    conn = get_conn()
    query = "SELECT q.*, w.alias FROM wallet_qualification q LEFT JOIN wallets w ON w.address = q.wallet_address"
    params = []
    if tier:
        query += " WHERE q.tier = ?"
        params.append(tier)
    query += f" ORDER BY q.{sort} DESC NULLS LAST LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    def pct(v):
        return round(v * 100, 1) if v is not None else None

    return [
        {
            "address": r["wallet_address"],
            "alias": r["alias"],
            "tier": r["tier"],
            "total_n": r["total_n"],
            "qualified_at_n": r["qualified_at_n"],
            "pre_qualification_n": r["pre_qualification_n"],
            "pre_qualification_win_rate": pct(r["pre_qualification_win_rate"]),
            "pre_qualification_wilson_low": pct(r["pre_qualification_wilson_low"]),
            "pre_qualification_total_return_pct": pct(r["pre_qualification_total_return_pct"]),
            "forward_n": r["forward_n"],
            "forward_win_rate": pct(r["forward_win_rate"]),
            "forward_wilson_low": pct(r["forward_wilson_low"]),
            "forward_total_return_pct": pct(r["forward_total_return_pct"]),
            "forward_median_return_pct": pct(r["forward_median_return_pct"]),
            "risk_discipline_score": round(r["risk_discipline_score"], 1) if r["risk_discipline_score"] is not None else None,
            "rfq_pct": pct(r["rfq_pct"]),
            "sizing_cv": round(r["sizing_cv"], 2) if r["sizing_cv"] is not None else None,
            "is_likely_bot": bool(r["is_likely_bot"]),
        }
        for r in rows
    ]


@app.get("/api/wallets/{address}/qualification-history")
def wallet_qualification_history(address: str):
    """scripts/build_wallet_qualification.py's history table (see
    conversation -- wallet_qualification itself is overwritten every run
    and can only ever show the current snapshot; this is the same rows
    over time, one per real requalification pass, so a wallet's tier and
    forward record can actually be tracked trending up or down rather than
    just read at a single point in time). Chronological, oldest first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM wallet_qualification_history WHERE wallet_address = ? ORDER BY computed_at ASC",
        (address,),
    ).fetchall()
    conn.close()

    def pct(v):
        return round(v * 100, 1) if v is not None else None

    return [
        {
            "computed_at": r["computed_at"],
            "tier": r["tier"],
            "total_n": r["total_n"],
            "qualified_at_n": r["qualified_at_n"],
            "forward_n": r["forward_n"],
            "forward_win_rate": pct(r["forward_win_rate"]),
            "forward_wilson_low": pct(r["forward_wilson_low"]),
            "forward_total_return_pct": pct(r["forward_total_return_pct"]),
            "forward_median_return_pct": pct(r["forward_median_return_pct"]),
            "risk_discipline_score": round(r["risk_discipline_score"], 1) if r["risk_discipline_score"] is not None else None,
        }
        for r in rows
    ]


@app.post("/api/wallets/{address}/feature")
def feature_wallet(address: str, payload: dict = {}):
    conn = get_conn()
    if conn.execute("SELECT 1 FROM wallets WHERE address = ?", (address,)).fetchone() is None:
        conn.close()
        raise HTTPException(404, "wallet not found")
    conn.execute(
        "INSERT OR IGNORE INTO featured_wallets (address, note, added_at) VALUES (?, ?, ?)",
        (address, payload.get("note"), int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/watchlist")
def get_watchlist():
    """Wallets scripts/run_watchlist_agent.py actively mirrors -- a frozen,
    curated table (see db/schema.sql), not a live read of copy_candidates,
    so a wallet dropping off the automated screen doesn't silently orphan
    an open paper position tied to it."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT wl.*, w.alias FROM watchlist wl
        LEFT JOIN wallets w ON w.address = wl.wallet_address
        ORDER BY wl.is_active DESC, wl.added_at DESC
        """
    ).fetchall()
    conn.close()
    return [
        {
            "address": r["wallet_address"],
            "alias": r["alias"],
            "added_at": r["added_at"],
            "source": r["source"],
            "added_reason": r["added_reason"],
            "is_active": bool(r["is_active"]),
            "removed_at": r["removed_at"],
            "removed_reason": r["removed_reason"],
            "last_checked_ts": r["last_checked_ts"],
            "notes": r["notes"],
        }
        for r in rows
    ]


@app.post("/api/watchlist")
def add_to_watchlist(payload: dict):
    """Manual add, same source/manual distinction the featured_wallets
    precedent already established for wallets pinned by hand vs by the
    automated screen.

    Always qualifies the wallet as part of adding it (see conversation:
    "if we add it manually, then you should perform a walkforward backtest
    on it regardless") -- scripts/qualify_single_wallet.py runs the same
    no-look-ahead qualification walk as the batch job
    (analysis/qualification.py), scoped to just this wallet, so its tier
    reflects real evidence immediately rather than waiting for the next
    scheduled batch rebuild. This makes the request slower (~15-20s under
    normal load, dominated by re-simulating all copy trades) but it's a
    deliberate, infrequent action, not a hot path."""
    address = payload.get("address")
    if not address:
        raise HTTPException(400, "address is required")
    conn = get_conn()
    if conn.execute("SELECT 1 FROM wallets WHERE address = ?", (address,)).fetchone() is None:
        conn.close()
        raise HTTPException(404, "wallet not found")
    now_ms = int(time.time() * 1000)
    conn.execute(
        """
        INSERT INTO watchlist (wallet_address, added_at, source, added_reason, is_active, last_checked_ts)
        VALUES (?, ?, 'manual', ?, 1, ?)
        ON CONFLICT (wallet_address) DO UPDATE SET is_active = 1, removed_at = NULL, removed_reason = NULL
        """,
        (address, now_ms, payload.get("reason"), now_ms),
    )
    conn.commit()

    from scripts.qualify_single_wallet import run_for_wallet
    result = run_for_wallet(conn, address)
    conn.close()

    return {
        "ok": True,
        "qualification": {
            "tier": result["tier"],
            "total_n": result["total_n"],
            "qualified_at_n": result["qualified_at_n"],
            "forward": result["forward"],
        },
    }


@app.delete("/api/watchlist/{address}")
def remove_from_watchlist(address: str, reason: str | None = None):
    conn = get_conn()
    now_ms = int(time.time() * 1000)
    conn.execute(
        "UPDATE watchlist SET is_active = 0, removed_at = ?, removed_reason = ? WHERE wallet_address = ?",
        (now_ms, reason, address),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


def paper_trade_to_dict(r):
    return {
        "id": r["id"],
        "source_wallet_address": r["source_wallet_address"],
        "alias": r["alias"] if "alias" in r.keys() else None,
        "instrument": r["instrument"],
        "asset": r["asset"],
        "option_type": r["option_type"],
        "strike": r["strike"],
        "expiry_date": datetime.fromtimestamp(r["expiry"], tz=timezone.utc).strftime("%Y-%m-%d"),
        "side": r["side"],
        "mode": r["mode"],
        "intended_price": r["intended_price"],
        "entry_price": r["entry_price"],
        "entry_ts": r["entry_ts"],
        "fill_status": r["fill_status"],
        "resolved_ts": r["resolved_ts"],
        "exit_price": r["exit_price"],
        "return_pct": round(r["return_pct"] * 100, 1) if r["return_pct"] is not None else None,
        "hypothetical_income_usd": round(r["return_pct"] * HYPOTHETICAL_STAKE_USD, 2) if r["return_pct"] is not None else None,
        "loss_capped": bool(r["loss_capped"]) if "loss_capped" in r.keys() and r["loss_capped"] is not None else False,
        "wallet_exit_price": r["wallet_exit_price"],
        "wallet_return_pct": round(r["wallet_return_pct"] * 100, 1) if r["wallet_return_pct"] is not None else None,
        "wallet_pnl_is_estimated": bool(r["wallet_pnl_is_estimated"]) if r["wallet_pnl_is_estimated"] is not None else None,
        "edge_bucket_n": r["edge_bucket_n"],
        "edge_bucket_win_rate": round(r["edge_bucket_win_rate"] * 100, 1) if r["edge_bucket_win_rate"] is not None else None,
        "edge_bucket_wilson_low": round(r["edge_bucket_wilson_low"] * 100, 1) if r["edge_bucket_wilson_low"] is not None else None,
        "edge_bucket_median_return_pct": round(r["edge_bucket_median_return_pct"] * 100, 1) if r["edge_bucket_median_return_pct"] is not None else None,
        "created_at": r["created_at"],
    }


@app.get("/api/paper-trades")
def list_paper_trades(status: str | None = None, limit: int = 200):
    conn = get_conn()
    query = """
        SELECT pt.*, w.alias FROM paper_trades pt
        LEFT JOIN wallets w ON w.address = pt.source_wallet_address
    """
    params = []
    if status:
        query += " WHERE pt.fill_status = ?"
        params.append(status)
    query += " ORDER BY pt.created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [paper_trade_to_dict(r) for r in rows]


@app.get("/api/paper-trades/summary")
def paper_trades_summary():
    """Overall track record plus a per-source-wallet breakdown -- the
    actual answer to "does copying these wallets work going forward,"
    updated by scripts/run_watchlist_agent.py as new trades resolve. Both
    'resolved' (ran to actual expiry -- the only path going forward) and
    the historical 'stopped_out' status (closed early by the now-retired
    live stop-loss engine, see conversation) have a real, determined
    outcome and count toward win_rate/total_return -- excluding either
    would hide cut losses and make the track record look artificially
    rosy. resolved_count/stopped_out_count are still reported separately
    so it's visible how positions actually ended. loss_capped_count is the
    replacement mechanism's footprint: how many naked shorts had their
    settlement return_pct floored at config.NAKED_SHORT_LOSS_FLOOR_PCT."""
    conn = get_conn()
    completed = conn.execute("SELECT * FROM paper_trades WHERE fill_status IN ('resolved', 'stopped_out')").fetchall()
    resolved_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE fill_status = 'resolved'").fetchone()[0]
    stopped_out_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE fill_status = 'stopped_out'").fetchone()[0]
    loss_capped_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE loss_capped = 1").fetchone()[0]
    open_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE fill_status = 'open'").fetchone()[0]
    pending_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE fill_status = 'pending_entry'").fetchone()[0]
    skipped_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE fill_status = 'skipped_stale'").fetchone()[0]
    skipped_naked_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE fill_status = 'skipped_naked'").fetchone()[0]
    skipped_weak_edge_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE fill_status = 'skipped_weak_edge'").fetchone()[0]

    wins = [r for r in completed if r["return_pct"] > 0]
    by_wallet = {}
    for r in completed:
        by_wallet.setdefault(r["source_wallet_address"], []).append(r["return_pct"])

    conn2 = get_conn()
    wallet_breakdown = []
    for addr, returns in by_wallet.items():
        alias_row = conn2.execute("SELECT alias FROM wallets WHERE address = ?", (addr,)).fetchone()
        n = len(returns)
        w = sum(1 for x in returns if x > 0)
        wallet_breakdown.append({
            "address": addr,
            "alias": alias_row["alias"] if alias_row else None,
            "resolved": n,
            "win_rate": round(w / n * 100, 1),
            "total_return_pct": round(sum(returns) * 100, 1),
            "hypothetical_income_usd": round(sum(returns) * HYPOTHETICAL_STAKE_USD, 2),
            "hypothetical_income_usd_small": round(sum(returns) * HYPOTHETICAL_STAKE_USD_SMALL, 2),
        })
    conn2.close()
    wallet_breakdown.sort(key=lambda w: w["total_return_pct"], reverse=True)

    # Us vs. the source wallets. "Us" is deliberately the SAME full
    # `completed` population as the top-line stats above -- our side of
    # every trade is already fully known the moment it resolves or stops
    # out, no reason to wait on anything else. "Source wallets" is
    # whatever subset of that same population also has a determined
    # wallet_return_pct -- necessarily smaller and growing over time,
    # since a stopped-out position resolves for us the instant the stop
    # fires while the wallet's own unprotected position only resolves at
    # its real, later expiry (see conversation -- that's not a bug, it's
    # the entire point of a stop-loss: we resolve faster than the
    # underlying position's natural timeline). The two counts
    # (our_n vs wallet_known_n) are reported separately and will usually
    # differ; that's expected, not a mismatch to fix.
    wallet_known = [r for r in completed if r["wallet_return_pct"] is not None]
    conn.close()

    wallet_wins_known = sum(1 for r in wallet_known if r["wallet_return_pct"] > 0)
    n_wallet_known = len(wallet_known)

    return {
        "resolved_count": resolved_count,
        "stopped_out_count": stopped_out_count,
        "loss_capped_count": loss_capped_count,
        "open_count": open_count,
        "pending_count": pending_count,
        "skipped_stale_count": skipped_count,
        "skipped_naked_count": skipped_naked_count,
        "skipped_weak_edge_count": skipped_weak_edge_count,
        "win_rate": round(len(wins) / len(completed) * 100, 1) if completed else None,
        "total_return_pct": round(sum(r["return_pct"] for r in completed) * 100, 1) if completed else None,
        "total_hypothetical_income_usd": round(sum(r["return_pct"] for r in completed) * HYPOTHETICAL_STAKE_USD, 2) if completed else None,
        "total_hypothetical_income_usd_small": round(sum(r["return_pct"] for r in completed) * HYPOTHETICAL_STAKE_USD_SMALL, 2) if completed else None,
        "hypothetical_stake_usd": HYPOTHETICAL_STAKE_USD,
        "hypothetical_stake_usd_small": HYPOTHETICAL_STAKE_USD_SMALL,
        "by_wallet": wallet_breakdown,
        "wallet_comparison": {
            "our_n": len(completed),
            "our_win_rate": round(len(wins) / len(completed) * 100, 1) if completed else None,
            "our_total_return_pct": round(sum(r["return_pct"] for r in completed) * 100, 1) if completed else None,
            "our_total_hypothetical_income_usd": round(sum(r["return_pct"] for r in completed) * HYPOTHETICAL_STAKE_USD, 2) if completed else None,
            "our_total_hypothetical_income_usd_small": round(sum(r["return_pct"] for r in completed) * HYPOTHETICAL_STAKE_USD_SMALL, 2) if completed else None,
            "wallet_known_n": n_wallet_known,
            "wallet_win_rate": round(wallet_wins_known / n_wallet_known * 100, 1) if n_wallet_known else None,
            "wallet_total_return_pct": round(sum(r["wallet_return_pct"] for r in wallet_known) * 100, 1) if n_wallet_known else None,
            "wallet_total_hypothetical_income_usd": round(sum(r["wallet_return_pct"] for r in wallet_known) * HYPOTHETICAL_STAKE_USD, 2) if n_wallet_known else None,
            "wallet_total_hypothetical_income_usd_small": round(sum(r["wallet_return_pct"] for r in wallet_known) * HYPOTHETICAL_STAKE_USD_SMALL, 2) if n_wallet_known else None,
        },
    }


@app.get("/api/wallets/search")
def search_wallets(
    q: str | None = None,
    asset: str | None = None,
    min_trades: int = 0,
    min_notional: float = 0,
    min_win_rate: float | None = None,
    exclude_bots: bool = True,
    featured_only: bool = False,
    sort: str = "notional",
    limit: int = 50,
    offset: int = 0,
):
    """Search across every wallet the backfill has ever seen (7,700+, vs.
    the ~74 with a flagged event that /api/wallets scopes to) -- the
    search-first replacement for that curated list. asset filters on
    wallet_leaderboard.btc_pct (fraction of legs in BTC) since that's
    already computed rather than an EXISTS subquery per row. Wallets with
    no leaderboard row (no resolved positions yet) still match a plain q
    search but won't pass any stat-based filter, same as they wouldn't
    have a stat to filter on."""
    if sort not in ("notional", "win_rate", "pnl", "trades"):
        raise HTTPException(400, "sort must be one of: notional, win_rate, pnl, trades")

    conn = get_conn()
    query = """
        SELECT w.address, w.alias, w.first_seen, w.label,
               l.positions, l.win_rate, l.net_of_fees, l.avg_notional_usd, l.is_likely_bot,
               l.call_pct, l.buy_pct, l.btc_pct,
               (fw.address IS NOT NULL) AS is_featured
        FROM wallets w
        LEFT JOIN wallet_leaderboard l ON l.wallet_address = w.address
        LEFT JOIN featured_wallets fw ON fw.address = w.address
        WHERE 1=1
    """
    params = []
    if q:
        query += " AND (w.address LIKE ? OR w.alias LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    if asset == "BTC":
        query += " AND l.btc_pct > 0"
    elif asset == "ETH":
        query += " AND l.btc_pct < 1"
    if min_trades > 0:
        query += " AND l.positions >= ?"
        params.append(min_trades)
    if min_notional > 0:
        query += " AND l.avg_notional_usd >= ?"
        params.append(min_notional)
    if min_win_rate is not None:
        query += " AND l.win_rate >= ?"
        params.append(min_win_rate / 100)
    if exclude_bots:
        query += " AND (l.is_likely_bot IS NULL OR l.is_likely_bot = 0)"
    if featured_only:
        query += " AND fw.address IS NOT NULL"

    count_row = conn.execute(f"SELECT COUNT(*) AS n FROM ({query})", params).fetchone()
    total = count_row["n"]

    order_col = {
        "notional": "l.avg_notional_usd",
        "win_rate": "l.win_rate",
        "pnl": "l.net_of_fees",
        "trades": "l.positions",
    }[sort]
    query += f" ORDER BY is_featured DESC, {order_col} DESC NULLS LAST LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {
        "total": total,
        "results": [
            {
                "address": r["address"],
                "alias": r["alias"],
                "first_seen": day_str(r["first_seen"]),
                "label": r["label"],
                "positions": r["positions"],
                "win_rate": round(r["win_rate"] * 100, 1) if r["win_rate"] is not None else None,
                "net_of_fees": round(r["net_of_fees"], 2) if r["net_of_fees"] is not None else None,
                "avg_notional_usd": round(r["avg_notional_usd"], 2) if r["avg_notional_usd"] is not None else None,
                "is_likely_bot": bool(r["is_likely_bot"]) if r["is_likely_bot"] is not None else False,
                "is_featured": bool(r["is_featured"]),
                "call_pct": round(r["call_pct"] * 100, 1) if r["call_pct"] is not None else None,
                "buy_pct": round(r["buy_pct"] * 100, 1) if r["buy_pct"] is not None else None,
                "btc_pct": round(r["btc_pct"] * 100, 1) if r["btc_pct"] is not None else None,
            }
            for r in rows
        ],
    }


@app.get("/api/wallets")
def list_wallets(asset: str | None = None, limit: int = 100):
    """'Notable' wallets = wallets that appear in at least one flagged
    event, ranked by total notional across their flagged events -- plus
    any manually featured wallets (deep-dive subjects), pinned first
    regardless of ranking. A featured wallet with zero flagged events
    still shows up, just with zeroed stats -- pinning is a manual research
    call, not dependent on the detectors having fired."""
    conn = get_conn()

    featured_addresses = [r["address"] for r in conn.execute("SELECT address FROM featured_wallets ORDER BY added_at").fetchall()]
    featured_rows = []
    for addr in featured_addresses:
        row = conn.execute(WALLET_STATS_QUERY, (addr,)).fetchone()
        if row["wallet_address"] is None:
            alias_row = conn.execute("SELECT alias FROM wallets WHERE address = ?", (addr,)).fetchone()
            row = {
                "wallet_address": addr, "wallet_alias": alias_row["alias"] if alias_row else None,
                "flagged_event_count": 0, "total_notional_usd": None, "avg_anomaly_score": None,
                "first_flagged_ts": None, "last_flagged_ts": None, "assets": None,
                "large_trade_count": 0, "wallet_jump_count": 0,
            }
        featured_rows.append(row)

    query = """
        SELECT
            oe.wallet_address, w.alias AS wallet_alias, COUNT(*) AS flagged_event_count,
            SUM(oe.notional_usd) AS total_notional_usd, AVG(oe.anomaly_score) AS avg_anomaly_score,
            MIN(oe.timestamp) AS first_flagged_ts, MAX(oe.timestamp) AS last_flagged_ts,
            GROUP_CONCAT(DISTINCT oe.asset) AS assets,
            SUM(CASE WHEN oe.flag_reason LIKE '%large_trade%' THEN 1 ELSE 0 END) AS large_trade_count,
            SUM(CASE WHEN oe.flag_reason LIKE '%wallet_size_jump%' THEN 1 ELSE 0 END) AS wallet_jump_count
        FROM options_events oe
        LEFT JOIN wallets w ON w.address = oe.wallet_address
        WHERE oe.is_flagged = 1 AND oe.wallet_address IS NOT NULL
    """
    params = []
    if featured_addresses:
        placeholders = ",".join("?" * len(featured_addresses))
        query += f" AND oe.wallet_address NOT IN ({placeholders})"
        params.extend(featured_addresses)
    if asset:
        query += " AND oe.asset = ?"
        params.append(asset.upper())
    query += " GROUP BY oe.wallet_address ORDER BY total_notional_usd DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    mm_map = get_mm_map(conn, featured_addresses + [r["wallet_address"] for r in rows])
    conn.close()
    featured = [wallet_row_to_dict(row, featured=True, mm=mm_map.get(row["wallet_address"])) for row in featured_rows]
    return featured + [wallet_row_to_dict(r, mm=mm_map.get(r["wallet_address"])) for r in rows]


@app.get("/api/wallets/{address}")
def wallet_detail(address: str):
    conn = get_conn()
    wallet_row = conn.execute(
        "SELECT address, chain, first_seen, label, alias FROM wallets WHERE address = ?", (address,)
    ).fetchone()
    if wallet_row is None:
        conn.close()
        raise HTTPException(404, "wallet not found")

    total_count = conn.execute(
        "SELECT COUNT(*) FROM options_events WHERE wallet_address = ?", (address,)
    ).fetchone()[0]
    flagged = conn.execute(
        EVENTS_SELECT + " WHERE oe.wallet_address = ? AND oe.is_flagged = 1 ORDER BY oe.timestamp DESC LIMIT 200",
        (address,),
    ).fetchall()
    featured_row = conn.execute("SELECT note FROM featured_wallets WHERE address = ?", (address,)).fetchone()
    mm = get_mm_map(conn, [address]).get(address)
    conn.close()

    return {
        "address": wallet_row["address"],
        "alias": wallet_row["alias"],
        "chain": wallet_row["chain"],
        "first_seen": day_str(wallet_row["first_seen"]),
        "label": wallet_row["label"],
        "total_event_count": total_count,
        "events": [event_to_dict(r) for r in flagged],
        "featured": featured_row is not None,
        "featured_note": featured_row["note"] if featured_row else None,
        "is_likely_market_maker": bool(mm["is_likely_bot"]) if mm else False,
        "legs_per_position": round(mm["legs_per_position"], 1) if mm else None,
        "legs_per_day": round(mm["legs_per_day"], 1) if mm else None,
    }


@app.get("/api/wallets/{address}/ratings")
def wallet_ratings(address: str):
    """Every scoring system built for this wallet across the project,
    consolidated in one place -- each answers a genuinely different
    question, so they're kept as separate blocks rather than merged into
    one number (see each table's own schema comment in db/schema.sql for
    why):
      own_record -- wallet_leaderboard: this wallet's own realized P&L and
        win rate, trading at its own size and timing. Doesn't say anything
        about whether copying it would work.
      copy_backtest -- copy_trade_backtest: if a copier had mirrored every
        one of this wallet's opens at the next available market print
        (not this wallet's own fill), would it have been profitable? A
        wallet can have a great own_record and a poor copy_backtest, or
        vice versa.
      qualification -- wallet_qualification: analysis/qualification.py's
        no-look-ahead walk. tier + the forward-tested (post-qualification)
        record -- the genuine answer to "if we'd started following this
        wallet the moment it looked good, what would have happened next."
      edge_profile -- wallet_edge_profile: this wallet's copy_backtest
        record broken out by (asset, option_type, entry_side) instead of
        one blended number -- a wallet's edge is usually concentrated in a
        specific kind of trade, not uniform across everything it does."""
    conn = get_conn()
    if conn.execute("SELECT 1 FROM wallets WHERE address = ?", (address,)).fetchone() is None:
        conn.close()
        raise HTTPException(404, "wallet not found")

    lb = conn.execute("SELECT * FROM wallet_leaderboard WHERE wallet_address = ?", (address,)).fetchone()
    backtest = conn.execute("SELECT * FROM copy_trade_backtest WHERE wallet_address = ?", (address,)).fetchone()
    qual = conn.execute("SELECT * FROM wallet_qualification WHERE wallet_address = ?", (address,)).fetchone()
    edge_rows = conn.execute(
        "SELECT * FROM wallet_edge_profile WHERE wallet_address = ? ORDER BY n DESC", (address,)
    ).fetchall()
    conn.close()

    def pct(v):
        return round(v * 100, 1) if v is not None else None

    return {
        "own_record": None if lb is None else {
            "positions": lb["positions"],
            "resolved": lb["resolved"],
            "win_rate": pct(lb["win_rate"]),
            "net_of_fees": round(lb["net_of_fees"], 2),
            "avg_notional_usd": round(lb["avg_notional_usd"], 2) if lb["avg_notional_usd"] is not None else None,
            "is_likely_bot": bool(lb["is_likely_bot"]),
        },
        "copy_backtest": None if backtest is None else {
            "copy_positions": backtest["copy_positions"],
            "copy_win_rate": pct(backtest["copy_win_rate"]),
            "copy_win_rate_wilson_low": pct(backtest["copy_win_rate_wilson_low"]),
            "copy_total_return_pct": pct(backtest["copy_total_return_pct"]),
            "copy_avg_return_pct": pct(backtest["copy_avg_return_pct"]),
            "copy_median_return_pct": pct(backtest["copy_median_return_pct"]),
            "copy_worst_return_pct": pct(backtest["copy_worst_return_pct"]),
            "copy_best_return_pct": pct(backtest["copy_best_return_pct"]),
            "excluded_stale": backtest["excluded_stale"],
            "excluded_open": backtest["excluded_open"],
        },
        "qualification": None if qual is None else {
            "tier": qual["tier"],
            "total_n": qual["total_n"],
            "qualified_at_n": qual["qualified_at_n"],
            "pre_qualification_n": qual["pre_qualification_n"],
            "pre_qualification_win_rate": pct(qual["pre_qualification_win_rate"]),
            "pre_qualification_wilson_low": pct(qual["pre_qualification_wilson_low"]),
            "pre_qualification_total_return_pct": pct(qual["pre_qualification_total_return_pct"]),
            "forward_n": qual["forward_n"],
            "forward_win_rate": pct(qual["forward_win_rate"]),
            "forward_wilson_low": pct(qual["forward_wilson_low"]),
            "forward_total_return_pct": pct(qual["forward_total_return_pct"]),
            "forward_median_return_pct": pct(qual["forward_median_return_pct"]),
            "risk_discipline_score": round(qual["risk_discipline_score"], 1) if qual["risk_discipline_score"] is not None else None,
            "rfq_pct": pct(qual["rfq_pct"]),
            "sizing_cv": round(qual["sizing_cv"], 2) if qual["sizing_cv"] is not None else None,
        },
        "edge_profile": [
            {
                "asset": r["asset"],
                "option_type": r["option_type"],
                "entry_side": r["entry_side"],
                "n": r["n"],
                "win_rate": pct(r["win_rate"]),
                "wilson_low": pct(r["wilson_low"]),
                "median_return_pct": pct(r["median_return_pct"]),
                "avg_return_pct": pct(r["avg_return_pct"]),
            }
            for r in edge_rows
        ],
    }


@app.get("/api/wallets/{address}/portfolio")
def wallet_portfolio(address: str):
    """P&L analysis across every options position this wallet has ever
    held (not just flagged ones). Positions are grouped by instrument --
    each distinct strike+expiry+type the wallet traded counts as one
    position, however many fills it took to open or close. Derive computes
    realized_pnl per trade leg itself (confirmed non-zero mainly on closing
    legs), so this sums that directly rather than reconstructing FIFO cost
    basis by hand. Win rate excludes positions with zero net P&L (still
    open, or a wash) from the denominator -- they haven't resolved yet.

    by_entry_side: checked across several top wallets and found no genuine
    spot/DEX buy-sell activity on Derive Chain at all -- everything is
    bridging collateral, options trading, or staking (see conversation).
    So "success at buy/sells" is answered here by entry side instead:
    since a position's legs mix buy (open or close) and sell (open or
    close) depending on direction, a leg-level buy/sell win rate isn't
    meaningful, but grouping by which side OPENED the position is.

    Expiry-settlement estimate: Derive's realized_pnl on a trade leg is
    only ever non-zero when that leg closes an existing position -- a
    wallet that sells to open and never buys back (common for systematic
    option writers) gets realized_pnl=0 on every single leg forever, even
    though real money changed hands at expiry. Cash-flow conservation
    means total position P&L = sum(all leg premiums, sells positive/buys
    negative) + settlement value of whatever's left open - fees; for a
    fully-closed position (net_qty==0) this was verified to match Derive's
    own realized_pnl sum exactly (see conversation). So once an instrument
    has expired, net_pnl is computed this way directly instead of trusting
    realized_pnl -- using the nearest available index price (from any
    wallet's trades) as a stand-in for the actual settlement price, which
    Derive's trade API doesn't expose. Flagged per-position as
    pnl_is_estimated whenever that price stand-in was actually needed
    (net_qty != 0 at expiry)."""
    conn = get_conn()
    if conn.execute("SELECT 1 FROM wallets WHERE address = ?", (address,)).fetchone() is None:
        conn.close()
        raise HTTPException(404, "wallet not found")

    positions = compute_wallet_positions(conn, address)
    conn.close()

    resolved = [p for p in positions if p["is_resolved"]]
    wins = [p for p in resolved if p["net_pnl"] > 0]
    losses = [p for p in resolved if p["net_pnl"] < 0]
    total_pnl = sum(p["net_pnl"] for p in positions)
    total_fees = sum(p["total_fees"] for p in positions)
    ranked = sorted(resolved, key=lambda p: p["net_pnl"], reverse=True)

    by_side = {}
    for side in ("buy", "sell"):
        side_positions = [p for p in resolved if p["entry_side"] == side]
        side_wins = [p for p in side_positions if p["net_pnl"] > 0]
        by_side[side] = {
            "resolved": len(side_positions),
            "win_rate": round(len(side_wins) / len(side_positions) * 100, 1) if side_positions else None,
            "net_pnl": round(sum(p["net_pnl"] for p in side_positions), 2),
        }

    return {
        "address": address,
        "position_count": len(positions),
        "resolved_count": len(resolved),
        "open_or_flat_count": len(positions) - len(resolved),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / len(resolved) * 100, 1) if resolved else None,
        "total_realized_pnl": round(total_pnl, 2),
        "total_fees_paid": round(total_fees, 2),
        "net_of_fees": round(total_pnl - total_fees, 2),
        "avg_win": round(sum(p["net_pnl"] for p in wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(p["net_pnl"] for p in losses) / len(losses), 2) if losses else None,
        "by_entry_side": by_side,
        "best_positions": ranked[:5],
        "worst_positions": ranked[-5:][::-1] if len(ranked) > 5 else [],
        "positions": positions,
        "estimated_pnl_count": sum(1 for p in positions if p["pnl_is_estimated"]),
    }


@app.get("/api/wallets/{address}/positions/{instrument}/legs")
def wallet_position_legs(address: str, instrument: str):
    """Every individual fill making up one position (wallet + instrument),
    full detail per leg -- what the "expand" interaction on a position
    row fetches."""
    conn = get_conn()
    rows = conn.execute(
        EVENTS_SELECT + " WHERE oe.wallet_address = ? AND oe.instrument = ? ORDER BY oe.timestamp",
        (address, instrument),
    ).fetchall()
    conn.close()
    if not rows:
        raise HTTPException(404, "no legs found for this wallet + instrument")
    return [event_to_dict(r) for r in rows]


@app.get("/api/wallets/{address}/value-timeline")
def wallet_value_timeline(address: str):
    """NOT a true mark-to-market net worth -- that would need historical
    option pricing for still-open positions (unavailable, see
    clients/derive_client.py notes: Derive only exposes greeks/pricing on
    the live ticker, not historical trades) and USD conversion for every
    collateral token a wallet might post (WBTC, WstETH, HYPE, etc. --
    no historical price feed wired up for those). What's actually shown,
    both real and directly computable from data on hand: (1) cumulative
    P&L net of fees from resolved positions (real closing trades, or the
    expiry-settlement estimate for positions with none -- see
    compute_wallet_positions), bucketed by resolved_ts so a position's
    outcome lands on the timeline when it was actually decided rather
    than when it was opened, and (2) cumulative USDC bridge flow
    (deposits-withdrawals) -- USDC being the one collateral asset that's
    already ~1:1 USD, so it doesn't need a price feed to be meaningful.
    Both bucketed to daily resolution and forward-filled onto one merged
    timeline for charting."""
    conn = get_conn()
    wallet_row = conn.execute("SELECT id FROM wallets WHERE address = ?", (address,)).fetchone()
    if wallet_row is None:
        conn.close()
        raise HTTPException(404, "wallet not found")
    wallet_id = wallet_row["id"]

    if conn.execute("SELECT COUNT(*) AS n FROM wallet_activity WHERE wallet_id = ?", (wallet_id,)).fetchone()["n"] == 0:
        _sync_onchain_activity(conn, address, wallet_id)

    positions = compute_wallet_positions(conn, address)
    flow_rows = conn.execute(
        """
        SELECT timestamp, direction, amount FROM wallet_activity
        WHERE wallet_id = ? AND asset = 'USDC' AND activity_type IN ('deposit', 'withdrawal')
        ORDER BY timestamp
        """,
        (wallet_id,),
    ).fetchall()
    conn.close()

    daily_pnl = {}
    cum = 0.0
    for p in sorted((p for p in positions if p["is_resolved"]), key=lambda p: p["resolved_ts"]):
        cum += p["net_pnl"]
        daily_pnl[day_str(p["resolved_ts"])] = cum

    daily_flow = {}
    cum = 0.0
    for r in flow_rows:
        cum += r["amount"] if r["direction"] == "in" else -r["amount"]
        daily_flow[day_str(r["timestamp"])] = cum

    all_days = sorted(set(daily_pnl) | set(daily_flow))
    series = []
    last_pnl, last_flow = 0.0, 0.0
    for d in all_days:
        last_pnl = daily_pnl.get(d, last_pnl)
        last_flow = daily_flow.get(d, last_flow)
        series.append({
            "date": d,
            "timestamp": int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000),
            "cumulative_pnl_net_fees": round(last_pnl, 2),
            "cumulative_usdc_flow": round(last_flow, 2),
        })

    return {"address": address, "series": series}


@app.get("/api/wallets/{address}/onchain-activity")
def wallet_onchain_activity(address: str, refresh: bool = False):
    """Wallet activity beyond options trades, via Derive Chain's Blockscout
    explorer (explorer.derive.xyz) -- deposits, withdrawals, staking,
    transfers. Cached in wallet_activity after first pull since this is a
    slow paginated fetch (up to 50 pages) and historical activity doesn't
    change; pass refresh=true to re-pull.

    Classification note: most of what Blockscout reports for an active
    Derive trader is ERC-4337 smart-account mechanics tied to options
    trading itself (verifyAndMatch/sendBatch/handleOps -- margin and
    premium settlement), not external capital movement. Those are labeled
    'options_trade' and are already reflected in your options data; the
    'deposit'/'withdrawal' ones are the genuine bridge-level capital flows
    in and out of Derive."""
    conn = get_conn()
    wallet_row = conn.execute("SELECT id FROM wallets WHERE address = ?", (address,)).fetchone()
    if wallet_row is None:
        conn.close()
        raise HTTPException(404, "wallet not found")
    wallet_id = wallet_row["id"]

    existing = conn.execute("SELECT COUNT(*) AS n FROM wallet_activity WHERE wallet_id = ?", (wallet_id,)).fetchone()
    if refresh or existing["n"] == 0:
        _sync_onchain_activity(conn, address, wallet_id)

    rows = conn.execute(
        "SELECT * FROM wallet_activity WHERE wallet_id = ? ORDER BY timestamp DESC", (wallet_id,)
    ).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    for it in items:
        it["date"] = day_str(it["timestamp"])

    bridge = [it for it in items if it["activity_type"] in ("deposit", "withdrawal")]
    internal = [it for it in items if it["activity_type"] == "options_trade"]
    staking = [it for it in items if it["activity_type"] == "staking"]
    other = [it for it in items if it["activity_type"] not in ("deposit", "withdrawal", "options_trade", "staking")]

    return {
        "address": address,
        "total_count": len(items),
        "bridge_activity": bridge,
        "staking_activity": staking,
        "other_transfers": other,
        "internal_trade_settlement_count": len(internal),
    }


def _classify_transfer(item, address):
    method = item.get("method") or ""
    from_addr = (item.get("from") or {}).get("hash", "").lower()
    to_addr = (item.get("to") or {}).get("hash", "").lower()
    addr_lower = address.lower()
    direction = "out" if from_addr == addr_lower else ("in" if to_addr == addr_lower else None)
    counterparty = item.get("to") if direction == "out" else item.get("from")

    if method == "executeDepositIntent" or (direction == "in" and method == "handleOps" and "stDRV" not in (item.get("token") or {}).get("symbol", "")):
        activity_type = "deposit" if direction == "in" else "withdrawal"
    elif method in ("executeWithdrawIntentSocket",):
        activity_type = "withdrawal"
    elif "stDRV" in (item.get("token") or {}).get("symbol", "") or method == "handleOps":
        activity_type = "staking"
    elif method in ("verifyAndMatch", "atomicVerifyAndMatch", "sendBatch"):
        activity_type = "options_trade"
    else:
        activity_type = "transfer"

    return activity_type, direction, counterparty


def _sync_onchain_activity(conn, address, wallet_id):
    conn.execute("DELETE FROM wallet_activity WHERE wallet_id = ?", (wallet_id,))
    rows = []
    for item in iter_token_transfers(address, max_pages=50):
        activity_type, direction, counterparty = _classify_transfer(item, address)
        token = item.get("token") or {}
        decimals = int(token.get("decimals") or 18)
        raw_value = (item.get("total") or {}).get("value")
        amount = float(raw_value) / (10**decimals) if raw_value is not None else 0.0
        ts = int(datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
        rows.append((
            wallet_id,
            item.get("transaction_hash"),
            item.get("log_index"),
            ts,
            activity_type,
            item.get("method"),
            direction,
            token.get("symbol") or "?",
            amount,
            None,
            (counterparty or {}).get("hash"),
            (counterparty or {}).get("name"),
        ))
    conn.executemany(
        """
        INSERT OR IGNORE INTO wallet_activity
            (wallet_id, tx_hash, log_index, timestamp, activity_type, method,
             direction, asset, amount, usd_value, counterparty_address, counterparty_label)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


@app.get("/api/annotations")
def list_annotations(event_id: int):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM annotations WHERE related_event_id = ? AND related_event_table = 'options_events' "
        "ORDER BY added_at DESC",
        (event_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/annotations")
def create_annotation(payload: dict):
    event_id = payload.get("related_event_id")
    note = payload.get("note", "")
    if not event_id or not note:
        raise HTTPException(400, "related_event_id and note are required")

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO annotations
            (related_event_id, related_event_table, note, related_announcement_date,
             related_announcement_summary, outcome_direction, added_at)
        VALUES (?, 'options_events', ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            note,
            payload.get("related_announcement_date"),
            payload.get("related_announcement_summary"),
            payload.get("outcome_direction"),
            int(time.time() * 1000),
        ),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


def _google_news_rss(query, limit=25):
    resp = requests.get(
        "https://news.google.com/rss/search",
        params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
        timeout=15,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for item in root.findall(".//item")[:limit]:
        source_el = item.find("source")
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": item.findtext("link"),
            "pub_date": item.findtext("pubDate"),
            "source": source_el.text if source_el is not None else None,
        })
    return items


@app.get("/api/news")
def news(asset: str, date: str, window_days: int = 3, include_policy: bool = True):
    """Phase 4 news lookup: a convenience function to save manual
    searching, not an automated matcher -- pulls headlines from Google
    News RSS scoped to a date window via after:/before: search operators.
    You read and judge relevance yourself; no NLP/correlation logic.

    include_policy also runs a site:rollcall.com sub-query, tagged
    separately -- Roll Call covers Capitol Hill process (bills, hearings,
    the text of official remarks), a different and complementary angle to
    the market-reaction coverage general crypto-press search returns.
    Confirmed empirically: rollcall.com is indexed by Google News and the
    site: + after:/before: operators work together for historical windows."""
    if asset.upper() not in ASSET_NAMES:
        raise HTTPException(400, "asset must be BTC or ETH")

    center = datetime.strptime(date, "%Y-%m-%d")
    start = (center - timedelta(days=window_days)).strftime("%Y-%m-%d")
    end = (center + timedelta(days=window_days)).strftime("%Y-%m-%d")

    query = f"{ASSET_NAMES[asset.upper()]} after:{start} before:{end}"
    general = [{**it, "source_type": "general"} for it in _google_news_rss(query)]

    policy = []
    if include_policy:
        policy_query = f"(crypto OR bitcoin OR stablecoin OR tariff) site:rollcall.com after:{start} before:{end}"
        policy = [{**it, "source_type": "policy"} for it in _google_news_rss(policy_query, limit=10)]

    return {
        "query": query,
        "policy_query": policy_query if include_policy else None,
        "window": {"start": start, "end": end},
        "items": general + policy,
    }


CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "crypto", "ethereum", " eth ", "stablecoin", "genius act",
    "clarity act", "blockchain", "digital asset", "strategic reserve", "tariff",
]


@app.get("/api/trump-posts")
def trump_posts(start_date: str, end_date: str):
    """Trump's own Truth Social posts for a date window, via Roll Call's
    Factbase archive (rollcall.com/wp-json/factbase/v1/twitter). The
    archive has no working server-side date-range filter -- its UI shows a
    'Custom Date Range' picker, but the JS that would wire it to the API is
    dead code (confirmed by reading the plugin source). This instead
    binary-searches the date-descending pagination for the right page,
    then walks forward collecting posts inside the window -- ~11 round
    trips plus page-walking, observed ~30s end to end, so results are
    cached by (start_date, end_date) since these are fixed historical
    windows that won't change. Not filtered to crypto by the archive
    itself -- Trump posts about everything -- so each post is tagged
    is_crypto_related by a simple keyword match; all posts in the window
    are still returned; you judge relevance yourself."""
    conn = get_conn()
    cached = conn.execute(
        "SELECT items_json FROM trump_posts_cache WHERE start_date = ? AND end_date = ?",
        (start_date, end_date),
    ).fetchone()
    if cached:
        conn.close()
        return {"window": {"start": start_date, "end": end_date}, "items": json.loads(cached["items_json"]), "cached": True}

    posts = get_posts_for_window(start_date, end_date)
    items = []
    for p in posts:
        text_lower = p["text"].lower()
        items.append({
            "date": p["date"],
            "text": p["text"],
            "platform": p.get("platform"),
            "post_url": p.get("post_url"),
            "is_crypto_related": any(kw in text_lower for kw in CRYPTO_KEYWORDS),
        })
    items.sort(key=lambda x: (not x["is_crypto_related"], x["date"]))

    conn.execute(
        "INSERT OR REPLACE INTO trump_posts_cache (start_date, end_date, items_json, fetched_at) VALUES (?, ?, ?, ?)",
        (start_date, end_date, json.dumps(items), int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()
    return {"window": {"start": start_date, "end": end_date}, "items": items, "cached": False}
