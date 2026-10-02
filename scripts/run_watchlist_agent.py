"""One tick of the watchlist agent: ingest new trades, detect new
first-ever positions from active watchlist wallets, price entries for
anything still pending, resolve anything past expiry. Meant to run on a
schedule (~15 min, matching the cadence PROJECT_PLAN.md specified for the
originally-scoped live poll) rather than as a long-lived loop -- everything
needed between runs (ingest_state.last_ts, watchlist.last_checked_ts,
paper_trades.fill_status) is already persisted in SQLite, so a resident
process would only add its own crash/reboot supervision burden for no
benefit.

Mirror rule: only the first-ever trade for a given (wallet, instrument)
qualifies -- matches exactly what backtest_copy_trades.simulate_copy_trades
validated (it only ever scores a position's first leg as the entry), so
this doesn't extrapolate beyond backtested evidence into mirroring
incremental adds, reduces, or closes.

mode is always 'simulated' here -- see paper_trades table comment in
db/schema.sql. No credentials, no real orders; Phase 4 (real testnet
execution) is a separate, later, explicitly-gated addition.

Naked shorts are mirrored (not skipped) and held to expiry like every other
paper trade -- see record_live_marks() below for the live mark/Greeks
observability (collection only, no longer triggers an early exit -- see
conversation) and scripts/resolve_paper_trades.py's loss floor
(config.NAKED_SHORT_LOSS_FLOOR_PCT) for how a naked short's risk is bounded
instead: at settlement, not via a live poll trying to catch it mid-life."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.pnl import intrinsic_value, nearest_index_price
from clients.derive_client import get_ticker
from config import COPY_STALENESS_WINDOW_MS, SETTLEMENT_FRESHNESS_TOLERANCE_MS
from db.cloud_conn import INTEGRITY_ERRORS, get_live_conn
from scripts.assign_wallet_aliases import assign_missing
from scripts.live_poll import ingest
from scripts.resolve_paper_trades import resolve


def find_copy_entry_price(conn, instrument, wallet_address, after_ts, own_rfq_ids):
    """Live equivalent of backtest_copy_trades.next_other_wallet_print --
    first trade in this instrument by a DIFFERENT wallet, at or after
    after_ts, not sharing an rfq_id this wallet itself used for this
    instrument (the RFQ-artifact exclusion -- see analysis/pnl.py and
    backtest_copy_trades.py's module docstring for why), within the
    staleness window. Returns (price, timestamp) of that matched print --
    NOT the wall-clock time we happened to notice it, since when running
    against a backlog (or just a slow poll tick) those can differ by a lot
    and entry_ts should reflect when the copy price actually existed."""
    cutoff = after_ts + COPY_STALENESS_WINDOW_MS
    rows = conn.execute(
        """
        SELECT wallet_address, price, rfq_id, timestamp FROM options_events
        WHERE instrument = ? AND timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp ASC
        """,
        (instrument, after_ts, cutoff),
    ).fetchall()
    for r in rows:
        if r["wallet_address"] == wallet_address:
            continue
        if r["rfq_id"] and r["rfq_id"] in own_rfq_ids:
            continue
        return r["price"], r["timestamp"]
    return None, None


MIN_BUCKET_N = 5           # matches scripts/build_trade_confidence.py's floor -- below this a bucket is noise, not evidence
WEAK_EDGE_WILSON_BAR = 0.50  # worse than a coin flip, in a combo the wallet has actually done enough of to judge

# Buy-side uses a different gate entirely -- see analysis/qualification.py's
# buy_edge_lower_bound docstring and conversation. Win-rate/Wilson is the
# wrong lens for a bounded-loss/right-skewed-win payoff; these mirror that
# module's MIN_BUY_BUCKET_N/alpha defaults kept in sync with
# scripts/build_trade_confidence.py, which is what actually computes the
# pooled bucket these thresholds are checked against.
MIN_BUY_BUCKET_N = 20
BUY_EDGE_LCB_BAR = 0.0
BUY_EDGE_CONCENTRATION_CAP = 0.85


def lookup_edge_bucket(conn, wallet_address, asset, option_type, side):
    """This wallet's historical record in this exact (asset, option_type,
    side) bucket, as of the last scripts/build_trade_confidence.py run --
    see that script's docstring and paper_trades' edge_bucket_* columns in
    db/schema.sql. Also carries return_lcb/top1_gain_share, only ever
    non-NULL on the pooled option_type='ALL' buy-side bucket."""
    return conn.execute(
        "SELECT n, win_rate, wilson_low, median_return_pct, return_lcb, top1_gain_share "
        "FROM wallet_edge_profile WHERE wallet_address = ? AND asset = ? AND option_type = ? AND entry_side = ?",
        (wallet_address, asset, option_type, side),
    ).fetchone()


def classify_candidate(conn, wallet, c):
    """Decides what a newly-detected first-ever-trade candidate becomes:
    pending_entry (proceed as before, whether buy or sell -- naked shorts
    are mirrored and held to expiry with a settlement-time loss floor, see
    scripts/resolve_paper_trades.py), or skipped_weak_edge, assigned
    immediately at detection, never passing through pending_entry.

    SELL-side: unchanged from before -- wallet_edge_profile has n>=5
    history for this wallet's exact (asset, option_type, side) combo and
    its wilson_low is below 50%, worse than a coin flip in a combination
    the wallet has actually done enough of to judge.

    BUY-side: a different gate entirely (see conversation and
    analysis/qualification.buy_edge_lower_bound's docstring). Win-rate is
    the wrong lens for a bounded-loss/right-skewed-win payoff -- it would
    wrongly exclude real distributed edge (confirmed on wallet
    jagged-wolf-eb's real SOL buy-side record). Looks up the pooled
    (asset, 'ALL', 'buy') bucket instead and skips only if n>=20 AND
    (return_lcb <= 0 OR top1_gain_share >= 0.85) -- a conservative
    lower-confidence-bound on mean return, plus an independent check that
    the edge isn't just one outlier trade (confirmed on wallet
    onyx-wolf-73: a naive gate on raw average return would have wrongly
    passed it despite losing money on 69% of its trades).

    Both sides: a combo/bucket with no evidence yet, or below its
    respective minimum n, still passes through -- absence of evidence
    isn't evidence of badness, and gating on it would mean a wallet's edge
    in something new to them could never be discovered. Returns
    (fill_status, edge_row_or_None)."""
    if c["side"] == "sell":
        edge = lookup_edge_bucket(conn, wallet, c["asset"], c["option_type"], c["side"])
        if edge and edge["n"] >= MIN_BUCKET_N and edge["wilson_low"] < WEAK_EDGE_WILSON_BAR:
            return "skipped_weak_edge", edge
        return "pending_entry", None

    edge = lookup_edge_bucket(conn, wallet, c["asset"], "ALL", "buy")
    if (
        edge
        and edge["n"] >= MIN_BUY_BUCKET_N
        and (edge["return_lcb"] <= BUY_EDGE_LCB_BAR or edge["top1_gain_share"] >= BUY_EDGE_CONCENTRATION_CAP)
    ):
        return "skipped_weak_edge", edge
    return "pending_entry", None


def find_new_candidates(conn, cursor_column="last_checked_ts"):
    """For every active watchlist wallet, finds first-ever-trade candidates
    since that wallet's cursor, classifies each via classify_candidate(),
    and advances the cursor -- but does NOT insert anything into
    paper_trades. Returns a list of (wallet, candidate_row, status,
    edge_row_or_None).

    Extracted from detect_new_positions() below (see conversation) so
    scripts/run_live_agent.py can reuse the exact same mirror-rule/
    weak-edge-gate logic instead of a second, drifting copy of it --
    same reasoning as this project's other shared-logic modules (e.g.
    analysis/pnl.py). cursor_column defaults to 'last_checked_ts', the
    column detect_new_positions has always used; run_live_agent.py passes
    'live_last_checked_ts' instead -- a separate cursor is required, not
    optional: if both agents consumed candidates off the same column,
    whichever one happened to run first would silently advance it past a
    candidate before the other ever saw it. cursor_column is always one of
    these two fixed, hardcoded literals from our own callers, never
    external input, so interpolating it into the query is safe."""
    watchlist = conn.execute(f"SELECT wallet_address, {cursor_column} AS cursor_ts FROM watchlist WHERE is_active = 1").fetchall()
    now_ms = int(time.time() * 1000)
    results = []

    for wl in watchlist:
        wallet = wl["wallet_address"]
        since = wl["cursor_ts"] or 0
        candidates = conn.execute(
            """
            SELECT oe.* FROM options_events oe
            WHERE oe.wallet_address = ? AND oe.timestamp > ?
              AND NOT EXISTS (
                SELECT 1 FROM options_events prior
                WHERE prior.wallet_address = oe.wallet_address
                  AND prior.instrument = oe.instrument
                  AND prior.timestamp < oe.timestamp
              )
            ORDER BY oe.timestamp
            """,
            (wallet, since),
        ).fetchall()

        for c in candidates:
            status, edge = classify_candidate(conn, wallet, c)
            results.append((wallet, c, status, edge))

        conn.execute(f"UPDATE watchlist SET {cursor_column} = ? WHERE wallet_address = ?", (now_ms, wallet))

    conn.commit()
    return results


def detect_new_positions(conn):
    """Opens a paper_trades row (mode='simulated') for every new
    (wallet, instrument) first-ever trade seen since each active watchlist
    wallet's last_checked_ts -- classified via find_new_candidates() into
    pending_entry (proceeds to pricing as before, buy or sell) or an
    immediate skipped_weak_edge. Unchanged behavior from before this was
    split out of find_new_candidates() -- see conversation."""
    now_ms = int(time.time() * 1000)
    opened = 0
    skipped_weak_edge = 0

    for wallet, c, status, edge in find_new_candidates(conn, cursor_column="last_checked_ts"):
        try:
            conn.execute(
                """
                INSERT INTO paper_trades
                    (source_wallet_address, source_event_id, instrument, asset, option_type, strike, expiry,
                     side, mode, intended_price, fill_status,
                     edge_bucket_n, edge_bucket_win_rate, edge_bucket_wilson_low, edge_bucket_median_return_pct,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'simulated', ?, ?, ?, ?, ?, ?, ?)
                """,
                (wallet, c["id"], c["instrument"], c["asset"], c["option_type"], c["strike"], c["expiry"],
                 c["side"], c["price"], status,
                 edge["n"] if edge else None, edge["win_rate"] if edge else None,
                 edge["wilson_low"] if edge else None, edge["median_return_pct"] if edge else None,
                 now_ms),
            )
            if status == "pending_entry":
                opened += 1
            elif status == "skipped_weak_edge":
                skipped_weak_edge += 1
        except INTEGRITY_ERRORS:
            pass  # source_event_id UNIQUE constraint -- already have a row for this trade

    conn.commit()
    return opened, skipped_weak_edge


def advance_pending_entries(conn):
    """Retries pricing for pending_entry rows; promotes to open once a
    valid copy price is found, or to skipped_stale once the staleness
    window has fully elapsed with nothing found."""
    now_ms = int(time.time() * 1000)
    pending = conn.execute("SELECT * FROM paper_trades WHERE fill_status = 'pending_entry'").fetchall()
    advanced = 0

    for p in pending:
        source = conn.execute(
            "SELECT timestamp FROM options_events WHERE id = ?", (p["source_event_id"],)
        ).fetchone()
        entry_ref_ts = source["timestamp"]
        own_rfqs = {
            r["rfq_id"] for r in conn.execute(
                "SELECT DISTINCT rfq_id FROM options_events WHERE wallet_address = ? AND instrument = ? AND rfq_id IS NOT NULL",
                (p["source_wallet_address"], p["instrument"]),
            ).fetchall()
        }
        price, matched_ts = find_copy_entry_price(conn, p["instrument"], p["source_wallet_address"], entry_ref_ts, own_rfqs)

        if price is not None and price > 0:
            edge = lookup_edge_bucket(conn, p["source_wallet_address"], p["asset"], p["option_type"], p["side"])
            conn.execute(
                """
                UPDATE paper_trades
                SET fill_status = 'open', entry_price = ?, entry_ts = ?,
                    edge_bucket_n = ?, edge_bucket_win_rate = ?, edge_bucket_wilson_low = ?, edge_bucket_median_return_pct = ?
                WHERE id = ?
                """,
                (
                    price, matched_ts,
                    edge["n"] if edge else None,
                    edge["win_rate"] if edge else None,
                    edge["wilson_low"] if edge else None,
                    edge["median_return_pct"] if edge else None,
                    p["id"],
                ),
            )
            advanced += 1
        elif now_ms > entry_ref_ts + COPY_STALENESS_WINDOW_MS:
            conn.execute("UPDATE paper_trades SET fill_status = 'skipped_stale' WHERE id = ?", (p["id"],))
            advanced += 1

    conn.commit()
    return advanced


def _safe_float(v):
    return float(v) if v is not None else None


def record_live_marks(conn):
    """Live observability for every open naked short (side='sell') that
    hasn't reached its own expiry yet: polls the live Derive ticker once
    per distinct instrument (not once per position -- several open
    positions can share one) and records a paper_trade_marks snapshot for
    each -- including Greeks, collected prospectively from get_ticker's
    option_pricing block since it's already being fetched for the mark
    price. Collection only -- does NOT close anything (see conversation:
    this used to be a live stop-loss engine that force-closed a position
    once its mark reached config.STOP_MULTIPLE x entry_price, retired
    after two days of production data showed the 15-minute poll cadence
    couldn't catch a short-dated, high-gamma naked short before its mark
    gapped well past the intended cap -- 48 stopped-out trades averaged
    -63% realized loss, not the intended -10%, and every one of the
    subset with a checkable wallet-own outcome would have been a win if
    left alone). Naked-short risk is now bounded at settlement instead --
    see scripts/resolve_paper_trades.py's loss floor. A buy's loss is
    already capped at 100% of premium paid -- no snapshot needed.

    Already-expired-but-still-'open' rows are left alone here -- that's
    resolve()'s job via the settlement-price path, no live call needed for
    a position whose outcome is already determined. A per-instrument
    ticker failure is logged and skipped, not fatal to the tick -- retried
    next run, same resilience advance_pending_entries() already has for a
    copy-price that hasn't printed yet."""
    now_ms = int(time.time() * 1000)
    open_shorts = conn.execute(
        "SELECT * FROM paper_trades WHERE fill_status = 'open' AND side = 'sell' AND expiry * 1000 > ?", (now_ms,)
    ).fetchall()

    by_instrument = {}
    for p in open_shorts:
        by_instrument.setdefault(p["instrument"], []).append(p)

    marks_recorded = 0
    for instrument, rows in by_instrument.items():
        try:
            ticker = get_ticker(instrument)
        except Exception as e:
            print(f"live ticker fetch failed for {instrument}, skipping this tick: {e}")
            continue

        mark_price = ticker.get("mark_price")
        index_price = ticker.get("index_price")
        if mark_price is None:
            continue
        mark_price = float(mark_price)
        index_price = _safe_float(index_price)
        best_bid = _safe_float(ticker.get("best_bid_price"))
        best_ask = _safe_float(ticker.get("best_ask_price"))
        greeks = ticker.get("option_pricing") or {}
        delta = _safe_float(greeks.get("delta"))
        gamma = _safe_float(greeks.get("gamma"))
        theta = _safe_float(greeks.get("theta"))
        vega = _safe_float(greeks.get("vega"))
        iv = _safe_float(greeks.get("iv"))
        rho = _safe_float(greeks.get("rho"))

        for p in rows:
            unrealized_return_pct = (p["entry_price"] - mark_price) / p["entry_price"]
            dte_days = (p["expiry"] * 1000 - now_ms) / 86_400_000
            conn.execute(
                """
                INSERT INTO paper_trade_marks
                    (paper_trade_id, ts, mark_price, index_price, unrealized_return_pct,
                     best_bid_price, best_ask_price, delta, gamma, theta, vega, iv, rho, dte_days)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (p["id"], now_ms, mark_price, index_price, unrealized_return_pct,
                 best_bid, best_ask, delta, gamma, theta, vega, iv, rho, dte_days),
            )
            marks_recorded += 1

    conn.commit()
    return marks_recorded


def check_wallet_outcomes(conn):
    """What did the SOURCE wallet's own position (not our copy) actually
    close at -- independent of our own fill_status, so this fills in even
    for skipped/pending rows (their position happened regardless of
    whether we managed to mirror it). Mirrors compute_wallet_positions in
    api/main.py exactly, scoped to one (wallet, instrument): if the wallet
    closed it themselves via a real offsetting trade, wallet_exit_price is
    the volume-weighted price of those closing legs; if they never closed
    it and the instrument has since expired, it's the same
    nearest-index-price + intrinsic-value estimate every other expiry
    settlement in this project uses. Left alone (retried next tick) if the
    wallet's own position is still open and unexpired -- no live call
    needed, this only reads options_events already ingested."""
    now_ms = int(time.time() * 1000)
    candidates = conn.execute(
        "SELECT * FROM paper_trades WHERE wallet_return_pct IS NULL AND intended_price IS NOT NULL"
    ).fetchall()

    updated = 0
    for p in candidates:
        legs = conn.execute(
            "SELECT side, size, price, timestamp, asset, strike, option_type FROM options_events "
            "WHERE wallet_address = ? AND instrument = ? ORDER BY timestamp",
            (p["source_wallet_address"], p["instrument"]),
        ).fetchall()
        if not legs:
            continue

        entry_side = legs[0]["side"]
        net_qty = sum((l["size"] or 0) if l["side"] == "buy" else -(l["size"] or 0) for l in legs)
        is_closed = abs(net_qty) <= 1e-9

        wallet_exit_price = None
        wallet_pnl_is_estimated = 0
        if is_closed:
            closing_legs = [l for l in legs[1:] if l["side"] != entry_side]
            if closing_legs:
                total_size = sum(l["size"] or 0 for l in closing_legs)
                if total_size > 0:
                    wallet_exit_price = sum((l["price"] or 0) * (l["size"] or 0) for l in closing_legs) / total_size
        elif p["expiry"] * 1000 <= now_ms:
            spot, gap_ms = nearest_index_price(conn, p["asset"], p["expiry"] * 1000)
            if spot is not None and gap_ms <= SETTLEMENT_FRESHNESS_TOLERANCE_MS:
                wallet_exit_price = intrinsic_value(spot, p["strike"], p["option_type"])
                wallet_pnl_is_estimated = 1
            # else: no sample close enough to the real expiry moment to trust yet -- recheck next tick
        # else: wallet's own position still open and unexpired -- recheck next tick

        if wallet_exit_price is not None and wallet_exit_price >= 0:
            if entry_side == "buy":
                wallet_return_pct = (wallet_exit_price - p["intended_price"]) / p["intended_price"]
            else:
                wallet_return_pct = (p["intended_price"] - wallet_exit_price) / p["intended_price"]
            conn.execute(
                "UPDATE paper_trades SET wallet_exit_price = ?, wallet_return_pct = ?, wallet_pnl_is_estimated = ? WHERE id = ?",
                (wallet_exit_price, wallet_return_pct, wallet_pnl_is_estimated, p["id"]),
            )
            updated += 1

    conn.commit()
    return updated


def run():
    conn = get_live_conn()
    # Meaningful locally (SQLite, single-writer file lock -- this runs
    # unattended every 15 min while the API server or a manual script can
    # also be writing); a no-op against the cloud DB (db/cloud_conn.py's
    # CloudConnection), where Postgres's real client-server MVCC makes the
    # whole lock-contention failure mode this guards against moot.
    conn.execute("PRAGMA busy_timeout = 8000")

    n_ingested = ingest(conn)
    assign_missing(conn)
    n_opened, n_skipped_weak_edge = detect_new_positions(conn)
    n_advanced = advance_pending_entries(conn)
    n_marks_recorded = record_live_marks(conn)
    n_resolved = resolve(conn)
    n_wallet_outcomes = check_wallet_outcomes(conn)

    conn.execute(
        """
        INSERT INTO agent_runs
            (run_ts, ingested_count, opened_count, advanced_count, resolved_count,
             skipped_weak_edge_count, stopped_out_count)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """,
        (int(time.time() * 1000), n_ingested, n_opened, n_advanced, n_resolved,
         n_skipped_weak_edge),
    )
    conn.commit()

    print(
        f"ingested {n_ingested} trade leg(s), opened {n_opened} new position(s), "
        f"advanced {n_advanced} pending entr(y/ies), resolved {n_resolved} trade(s), "
        f"recorded {n_marks_recorded} live mark(s), skipped {n_skipped_weak_edge} weak-edge, "
        f"determined {n_wallet_outcomes} wallet-own outcome(s)"
    )
    conn.close()


if __name__ == "__main__":
    run()
