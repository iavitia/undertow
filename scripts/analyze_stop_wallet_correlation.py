"""Before considering wallet-specific stop thresholds (see conversation --
fitting a separate stop per wallet risks overfitting noise at the sample
sizes most watchlist wallets have, often just 30-300 resolved positions):
does being hurt by a tight stop correlate with anything already computed
about a wallet? If it does, that's evidence a wallet-specific approach
would find a real, stable signal. If nothing correlates, that's a reason
not to build the heavier per-wallet-fitting machinery yet.

Scoped to 'current_watchlist' only -- 'blown_up_8' is both too small (8
wallets) and too uniformly helped by any stop to be informative here.

Classification is a majority vote across the whole zoomed 1.1x-2.5x fine
grid (scripts/analyze_stop_sensitivity.FINE_MULTIPLES), not a
single-point read at one multiple -- consistent with "robust region" over
"one lucky number.\""""
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH
from scripts.analyze_stop_sensitivity import FINE_MULTIPLES, first_crossing, position_excursion_path
from scripts.backtest_copy_trades import build_spot_price_index, load_events, simulate_copy_trades
from scripts.backtest_stop_loss import MIN_ENTRY_PRICE

VOTE_THRESHOLD = 0.7  # >=70% of the 15 fine-grid multiples must agree to call a wallet consistently helped/hurt


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    watchlist_addrs = {
        r["wallet_address"] for r in conn.execute("SELECT wallet_address FROM watchlist WHERE is_active = 1").fetchall()
    }

    t0 = time.time()
    df = load_events(conn)
    print(f"loaded {len(df)} rows in {time.time()-t0:.1f}s")
    now_ms = int(time.time() * 1000)
    positions, _, _ = simulate_copy_trades(df, now_ms)
    print(f"simulated {len(positions)} copy trades in {time.time()-t0:.1f}s")
    spot_index = build_spot_price_index(df)

    target_positions = [
        p for p in positions
        if p["wallet_address"] in watchlist_addrs and p["entry_price"] >= MIN_ENTRY_PRICE[p["asset"]]
    ]

    # One excursion-path slice per position (the expensive part), all 15
    # fine-grid protected returns read off it -- same "slice once, read
    # many" principle as analyze_stop_sensitivity.py, not re-sliced per
    # multiple.
    by_wallet_actual_total = {}
    by_wallet_protected_total = {mult: {} for mult in FINE_MULTIPLES}
    for p in target_positions:
        wallet = p["wallet_address"]
        by_wallet_actual_total[wallet] = by_wallet_actual_total.get(wallet, 0.0) + p["return_pct"]

        if p["entry_side"] != "sell":
            for mult in FINE_MULTIPLES:
                by_wallet_protected_total[mult][wallet] = by_wallet_protected_total[mult].get(wallet, 0.0) + p["return_pct"]
            continue

        window_ts, values = position_excursion_path(
            spot_index, p["asset"], p["strike"], p["option_type"], p["entry_ts"], p["resolved_ts"]
        )
        for mult in FINE_MULTIPLES:
            protected_return = p["return_pct"]
            if len(values) > 0:
                _, stop_value = first_crossing(window_ts, values, mult * p["entry_price"])
                if stop_value is not None:
                    protected_return = (p["entry_price"] - stop_value) / p["entry_price"]
            by_wallet_protected_total[mult][wallet] = by_wallet_protected_total[mult].get(wallet, 0.0) + protected_return

    classifications = {}
    for wallet, actual_total in by_wallet_actual_total.items():
        helped_votes = sum(1 for mult in FINE_MULTIPLES if by_wallet_protected_total[mult][wallet] > actual_total)
        hurt_votes = sum(1 for mult in FINE_MULTIPLES if by_wallet_protected_total[mult][wallet] < actual_total)
        n = len(FINE_MULTIPLES)
        if helped_votes >= n * VOTE_THRESHOLD:
            classifications[wallet] = "consistently_helped"
        elif hurt_votes >= n * VOTE_THRESHOLD:
            classifications[wallet] = "consistently_hurt"
        else:
            classifications[wallet] = "mixed"

    # Pull already-computed wallet characteristics.
    qual_rows = {
        r["wallet_address"]: r for r in conn.execute(
            "SELECT wallet_address, risk_discipline_score, rfq_pct, sizing_cv, total_n, forward_n FROM wallet_qualification"
        ).fetchall()
    }
    mae_rows = {}
    for r in conn.execute(
        "SELECT wallet_address, outcome_group, median_mae_multiple, p90_mae_multiple FROM wallet_mae_summary WHERE wallet_set = 'current_watchlist'"
    ).fetchall():
        mae_rows.setdefault(r["wallet_address"], {})[r["outcome_group"]] = r

    now_ms2 = int(time.time() * 1000)
    conn.execute("DELETE FROM stop_wallet_correlation")
    rows_out = []
    for wallet, classification in classifications.items():
        q = qual_rows.get(wallet)
        winner = mae_rows.get(wallet, {}).get("winner")
        loser = mae_rows.get(wallet, {}).get("loser")
        rows_out.append((
            wallet, classification,
            q["risk_discipline_score"] if q else None,
            q["rfq_pct"] if q else None,
            q["sizing_cv"] if q else None,
            q["total_n"] if q else None,
            q["forward_n"] if q else None,
            winner["median_mae_multiple"] if winner else None,
            winner["p90_mae_multiple"] if winner else None,
            loser["median_mae_multiple"] if loser else None,
            loser["p90_mae_multiple"] if loser else None,
            now_ms2,
        ))
    conn.executemany(
        """
        INSERT INTO stop_wallet_correlation
            (wallet_address, classification, risk_discipline_score, rfq_pct, sizing_cv, total_n, forward_n,
             winner_median_mae, winner_p90_mae, loser_median_mae, loser_p90_mae, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_out,
    )
    conn.commit()

    counts = {}
    for c in classifications.values():
        counts[c] = counts.get(c, 0) + 1
    print(f"\nclassification counts: {counts}\n")

    field_names = [
        "risk_discipline_score", "rfq_pct", "sizing_cv", "total_n", "forward_n",
        "winner_median_mae", "winner_p90_mae", "loser_median_mae", "loser_p90_mae",
    ]
    field_idx = {name: 2 + i for i, name in enumerate(field_names)}

    print(f"{'characteristic':<24}{'helped':<20}{'hurt':<20}{'mixed':<20}")
    for name in field_names:
        idx = field_idx[name]
        by_class = {"consistently_helped": [], "consistently_hurt": [], "mixed": []}
        for row in rows_out:
            val = row[idx]
            if val is not None:
                by_class[row[1]].append(val)
        line = f"{name:<24}"
        for c in ("consistently_helped", "consistently_hurt", "mixed"):
            vals = by_class[c]
            line += (f"n={len(vals)} med={statistics.median(vals):.2f}" if vals else "n=0").ljust(20)
        print(line)

    conn.close()


if __name__ == "__main__":
    run()
