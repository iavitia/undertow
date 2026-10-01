import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.anomaly_detection import (
    compute_daily_signals,
    flag_large_trades,
    flag_wallet_jumps,
    load_all_trades,
    load_trades,
)
from config import DB_PATH

ASSETS = ("ETH", "BTC")


def pd_isna(x):
    return x != x  # NaN != NaN; avoids importing pandas here just for this


def apply_flags(conn, per_asset_large, wallet_jumps):
    """Combine both detectors per event id: is_flagged if either fires,
    flag_reason lists whichever did."""
    conn.execute("UPDATE options_events SET is_flagged = 0, flag_reason = NULL, "
                 "anomaly_score = NULL, wallet_jump_ratio = NULL WHERE source = 'derive'")

    large_by_id = {}
    for df in per_asset_large:
        for row in df[df["is_large_trade"]].itertuples():
            large_by_id[row.id] = row.size_zscore

    jump_by_id = {
        row.id: row.wallet_jump_ratio
        for row in wallet_jumps[wallet_jumps["is_wallet_jump"]].itertuples()
    }

    all_ids = set(large_by_id) | set(jump_by_id)
    rows = []
    for event_id in all_ids:
        reasons = []
        if event_id in large_by_id:
            reasons.append("large_trade")
        if event_id in jump_by_id:
            reasons.append("wallet_size_jump")
        rows.append((
            ",".join(reasons),
            large_by_id.get(event_id),
            jump_by_id.get(event_id),
            int(event_id),
        ))

    conn.executemany(
        "UPDATE options_events SET is_flagged = 1, flag_reason = ?, anomaly_score = ?, "
        "wallet_jump_ratio = ? WHERE id = ?",
        rows,
    )
    conn.commit()
    return large_by_id, jump_by_id


def apply_daily_signals(conn, asset, daily):
    conn.execute("DELETE FROM daily_signals WHERE asset = ?", (asset,))
    rows = [
        (
            asset,
            row.day.strftime("%Y-%m-%d"),
            float(row.total_notional),
            int(row.trade_count),
            float(row.skew),
            None if pd_isna(row.volume_zscore) else float(row.volume_zscore),
            None if pd_isna(row.skew_zscore) else float(row.skew_zscore),
            int(row.is_volume_spike),
            int(row.is_skew_shift),
        )
        for row in daily.itertuples()
    ]
    conn.executemany(
        """
        INSERT INTO daily_signals
            (asset, day, total_notional_usd, trade_count, call_put_skew,
             volume_zscore, skew_zscore, is_volume_spike, is_skew_shift)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def run():
    conn = sqlite3.connect(DB_PATH)

    per_asset_large = []
    for asset in ASSETS:
        print(f"\n=== {asset} ===")
        df = load_trades(conn, asset)
        df = flag_large_trades(df)
        per_asset_large.append(df)

        n_large = int(df["is_large_trade"].sum())
        print(f"large trades: {n_large} / {len(df)} ({n_large / len(df):.2%})")

        daily = compute_daily_signals(df)
        apply_daily_signals(conn, asset, daily)
        print(f"volume-spike days: {int(daily['is_volume_spike'].sum())} / {len(daily)}")
        print(f"skew-shift days: {int(daily['is_skew_shift'].sum())} / {len(daily)}")

    print("\n=== wallet-size-jump detector (cross-asset) ===")
    all_df = load_all_trades(conn)
    all_df = flag_wallet_jumps(all_df)
    n_jump = int(all_df["is_wallet_jump"].sum())
    print(f"wallet jumps: {n_jump} / {len(all_df)} ({n_jump / len(all_df):.3%})")

    large_by_id, jump_by_id = apply_flags(conn, per_asset_large, all_df)

    both = set(large_by_id) & set(jump_by_id)
    print(f"\ntotal flagged: {len(set(large_by_id) | set(jump_by_id))} "
          f"(large_trade: {len(large_by_id)}, wallet_size_jump: {len(jump_by_id)}, both: {len(both)})")

    top_jumps = all_df[all_df["is_wallet_jump"]].sort_values("wallet_jump_ratio", ascending=False).head(10)
    print("\ntop 10 wallet jumps:")
    for row in top_jumps.itertuples():
        print(
            f"  {row.day.date()}  {row.asset}  {row.instrument}  {row.side:<4}  "
            f"notional=${row.notional_usd:,.0f}  jump={row.wallet_jump_ratio:.1f}x  "
            f"wallet={row.wallet_address[:10]}.."
        )

    conn.execute("ANALYZE")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    run()
