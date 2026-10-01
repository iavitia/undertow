"""Composite copy-candidate screen, built on top of copy_trade_backtest
(full history) and walkforward_backtest (formation/validation split).

The walk-forward check confirmed win-rate persists strongly (r=+0.62) but
found several wallets with excellent, validated win rates that are still
net losers over their full history (brass-basin-71, glacial-canyon-f8,
lunar-otter-91, onyx-vault-bf): consistent small wins wiped out by rare,
disproportionate losses. Win rate alone cannot distinguish that shape from
a genuinely good copy target -- only checking total return alongside it
can. See conversation.

passes_screen requires ALL of:
  1. Validated edge: validation_win_rate beats the population's own average
     validation win rate (the same "top-quintile" bar from the walk-forward
     report, computed dynamically rather than hardcoded).
  2. Win rate > 50% in BOTH formation and validation (not just Wilson-positive).
  3. Positive total return in BOTH formation and validation -- the term that
     actually separates the good list from the trap list above.
  4. Not bot-like (wallet_leaderboard.is_likely_bot).

tail_ratio (full-history total_return_pct / |worst single-position return|)
is reported for every candidate, passing or not, as the explicit tail-risk
read the screen above doesn't fully capture on its own -- higher means the
wallet's cumulative edge outweighs its single worst print by more.
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_PATH


def run():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # this can now be triggered synchronously from a POST /api/watchlist
    # request while the scheduled agent is also writing -- wait briefly on
    # a lock instead of failing instantly (same as api/main.py's get_conn()).
    conn.execute("PRAGMA busy_timeout = 8000")

    wf_rows = conn.execute("SELECT * FROM walkforward_backtest").fetchall()
    if not wf_rows:
        print("walkforward_backtest is empty -- run scripts/backtest_walkforward.py first")
        conn.close()
        return

    population_avg_validation_win_rate = sum(r["validation_win_rate"] for r in wf_rows) / len(wf_rows)
    print(f"population average validation win rate: {population_avg_validation_win_rate*100:.1f}%")

    bot_map = {r["wallet_address"]: bool(r["is_likely_bot"]) for r in conn.execute("SELECT wallet_address, is_likely_bot FROM wallet_leaderboard")}
    full_map = {r["wallet_address"]: r for r in conn.execute("SELECT * FROM copy_trade_backtest")}

    conn.execute("DELETE FROM copy_candidates")
    now_ms = int(time.time() * 1000)
    rows_out = []
    n_pass = 0

    for wf in wf_rows:
        wallet = wf["wallet_address"]
        full = full_map.get(wallet)
        if full is None:
            continue

        formation_total_return = wf["formation_avg_return_pct"] * wf["formation_n"]
        validation_total_return = wf["validation_avg_return_pct"] * wf["validation_n"]
        is_bot = bot_map.get(wallet, False)

        worst = full["copy_worst_return_pct"]
        tail_ratio = (full["copy_total_return_pct"] / abs(worst)) if worst not in (None, 0) else None

        passes = (
            not is_bot
            and wf["validation_win_rate"] > population_avg_validation_win_rate
            and wf["formation_win_rate"] > 0.5
            and wf["validation_win_rate"] > 0.5
            and formation_total_return > 0
            and validation_total_return > 0
        )
        if passes:
            n_pass += 1

        rows_out.append((
            wallet,
            wf["formation_win_rate"], wf["formation_wilson_low"], wf["formation_n"], formation_total_return,
            wf["validation_win_rate"], wf["validation_wilson_low"], wf["validation_n"], validation_total_return,
            full["copy_positions"], full["copy_win_rate_wilson_low"], full["copy_total_return_pct"], worst,
            tail_ratio, int(is_bot), int(passes), now_ms,
        ))

    conn.executemany(
        """
        INSERT INTO copy_candidates
            (wallet_address, formation_win_rate, formation_wilson_low, formation_n, formation_total_return_pct,
             validation_win_rate, validation_wilson_low, validation_n, validation_total_return_pct,
             full_positions, full_wilson_low, full_total_return_pct, full_worst_return_pct, tail_ratio,
             is_likely_bot, passes_screen, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_out,
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    print(f"wrote {len(rows_out)} copy_candidates rows, {n_pass} pass the full screen")
    conn.close()


if __name__ == "__main__":
    run()
