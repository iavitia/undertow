"""Daily Hyperliquid paper run of a perp strategy: trend breakout on BTC,
with idle capital earning funding carry. No orders are placed; it keeps a
paper equity index from Hyperliquid's own prices, funding and fees.

The strategy (fixed in advance, not tuned):
  - Trend: go long when the daily close makes a new 20-day high; exit when
    it makes a new 10-day low. While long, size the perp so the position
    targets 20% annualized volatility (30-day realized), capped at 2x
    equity.
  - Carry: while the trend is flat, hold long spot + short perp on
    1 / CARRY_CAPITAL_PER_NOTIONAL of equity, collecting funding.
A backtest over 2023-06 -> 2026-10 with Hyperliquid funding and fees made
~+12.8%/yr with a -12% max drawdown. It was strong in trending years
(2024 +29.5%) and trailed plain carry in choppy ones (2025 +3.6% vs +7.3%).

Accounting for each closed UTC day: P&L = trend_pos x perp return
- trend_pos x funding + carry_pos x funding (funding = sum of that day's
hourly rates; positive means longs pay shorts), then fees on any position
change: perp taker 0.045% + 0.01% slippage, plus the spot leg 0.07% +
0.01% when carry changes. Carry assumes the spot and perp legs offset
exactly (no basis P&L).

Run from scripts/hl_recorder.py once a day. Each run processes every
closed day since the last recorded one, so a missed run catches up.
"""
import math
import statistics as st
import time

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
STRATEGY = "btc-breakout20-10-vol20-carry"
COIN = "BTC"
ENTRY_LOOKBACK, EXIT_LOOKBACK, VOL_LOOKBACK = 20, 10, 30
VOL_TARGET, MAX_LEV = 0.20, 2.0
CARRY_CAPITAL_PER_NOTIONAL = 1.5
PERP_COST = 0.00045 + 0.0001
SPOT_COST = 0.0007 + 0.0001
_DAY_MS = 86_400_000


def _info(body, max_retries=4):
    for attempt in range(max_retries):
        try:
            r = requests.post(INFO_URL, json=body, timeout=60)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.HTTPError):
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))


def daily_closes(now_ms, days=90):
    """{day_start_ms: close} for fully closed UTC days only."""
    candles = _info({"type": "candleSnapshot", "req": {"coin": COIN, "interval": "1d",
                                                       "startTime": now_ms - days * _DAY_MS, "endTime": now_ms}})
    today = now_ms // _DAY_MS * _DAY_MS
    return {int(c["t"]): float(c["c"]) for c in candles if int(c["t"]) < today}


def funding_for_day(day_ms):
    rates = _info({"type": "fundingHistory", "coin": COIN, "startTime": day_ms, "endTime": day_ms + _DAY_MS - 1})
    return sum(float(r["fundingRate"]) for r in rates)


def decide(day, closes_by_day, was_in_trend):
    """(in_trend, trend_pos, carry_pos, vol) at `day`'s close, using only
    closes up to and including it."""
    days = sorted(d for d in closes_by_day if d <= day)
    c = [closes_by_day[d] for d in days]
    if len(c) < VOL_LOOKBACK + 1:
        return was_in_trend, 0.0, 1.0 / CARRY_CAPITAL_PER_NOTIONAL, None
    last, prior = c[-1], c[:-1]
    in_trend = was_in_trend
    if last >= max(prior[-ENTRY_LOOKBACK:]):
        in_trend = True
    elif last <= min(prior[-EXIT_LOOKBACK:]):
        in_trend = False
    rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - VOL_LOOKBACK, len(c))]
    vol = st.pstdev(rets) * math.sqrt(365)
    if in_trend:
        return True, min(MAX_LEV, VOL_TARGET / vol) if vol else 1.0, 0.0, vol
    return False, 0.0, 1.0 / CARRY_CAPITAL_PER_NOTIONAL, vol


def run_daily(conn, now_ms=None):
    """Records every closed day not yet recorded. Returns days recorded."""
    now_ms = now_ms or int(time.time() * 1000)
    closes = daily_closes(now_ms)
    last = conn.execute(
        "SELECT * FROM hl_paper_strategy WHERE strategy = ? ORDER BY day DESC LIMIT 1", (STRATEGY,)
    ).fetchone()
    recorded = 0
    if last is None:
        # first run: open at the latest close with equity 1.0, no P&L yet
        day = max(closes)
        in_trend, tp, cp, vol = decide(day, closes, False)
        _insert(conn, day, closes[day], None, None, in_trend, tp, cp, vol, 0.0, 1.0)
        return 1
    prev_day, prev_close = last["day"], last["close"]
    in_trend, tp, cp, eq = bool(last["in_trend"]), last["trend_pos"], last["carry_pos"], last["equity"]
    for day in sorted(d for d in closes if d > prev_day):
        ret = closes[day] / prev_close - 1
        f = funding_for_day(day)
        eq *= 1 + tp * ret - tp * f + cp * f
        new_in, new_tp, new_cp, vol = decide(day, closes, in_trend)
        fee = abs(new_tp - tp) * PERP_COST + abs(new_cp - cp) * (PERP_COST + SPOT_COST)
        eq *= 1 - fee
        _insert(conn, day, closes[day], ret, f, new_in, new_tp, new_cp, vol, fee, eq)
        in_trend, tp, cp, prev_close = new_in, new_tp, new_cp, closes[day]
        recorded += 1
    return recorded


def _insert(conn, day, close, ret, funding, in_trend, tp, cp, vol, fee, eq):
    conn.execute(
        """
        INSERT INTO hl_paper_strategy
            (strategy, day, close, day_return, funding, in_trend, trend_pos, carry_pos, vol_30d, fee_pct, equity, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (strategy, day) DO NOTHING
        """,
        (STRATEGY, day, close, ret, funding, int(in_trend), tp, cp, vol, fee, eq, int(time.time() * 1000)),
    )
