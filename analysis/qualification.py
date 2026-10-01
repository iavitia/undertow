"""No-look-ahead wallet qualification -- replaces the fixed-split
walk-forward design (walkforward_backtest / copy_candidates) with
something that directly answers "if we'd started copying this wallet the
moment it looked good, what would have happened since."

Walks a wallet's resolved copy-trade positions in chronological order,
computing cumulative stats using ONLY positions up to each point. The
first point where that cumulative record clears an absolute bar --
QUALIFY_WILSON_BAR win-rate confidence AND positive cumulative return,
both using only data available at that point -- is the wallet's
"qualification point." Everything strictly after that point is the
forward record: a genuine simulation of "we found this wallet at this
moment and started copying it," not a retrospective comparison of two
arbitrary halves.

This fixes two flaws in the fixed-split design (see conversation):
  1. A wallet whose entire history starts after a fixed global cutoff had
     zero data on one side and could never be evaluated, regardless of
     quality (drift-vault-68 was the case that surfaced this).
  2. Comparing two arbitrary halves doesn't ask "would we have actually
     selected this wallet at the time" -- it can show apparent
     persistence with no real point-in-time decision behind it.

QUALIFY_WILSON_BAR=0.60 is calibrated against the real population, not a
round guess: among wallets with 15+ copy-eligible positions, a 60% Wilson
lower bound is the top ~7% (median is 22.9%, p90 is 52.4%) -- genuinely
selective, not a token bar.

Deliberately lean for v1 (see conversation): one new synthesized score
(risk_discipline_score, combining tail ratio / RFQ-cleanliness / sizing
consistency) rather than a full multi-axis system with unvalidated
formulas. Everything else is shown as the real underlying numbers
(n, Wilson bounds, returns) rather than compressed into more invented
scores.
"""
import math
from statistics import NormalDist

import numpy as np

MIN_QUALIFY_N = 15          # don't even consider qualifying below this -- avoids qualifying on early luck
QUALIFY_WILSON_BAR = 0.60   # calibrated: top ~7% of wallets with 15+ positions (see module docstring)
MIN_FORWARD_N = 15          # forward sample needed to call a qualification "validated" rather than merely "provisional"
HIGH_CONFIDENCE_TOTAL_N = 75
HIGH_CONFIDENCE_FORWARD_N = 30


def wilson_lower_bound(wins, n, z=1.96):
    if n == 0:
        return None
    phat = wins / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return (center - margin) / denom


def _t_ppf(p, df):
    """Student's t quantile function, approximated from the standard normal
    quantile via the Cornish-Fisher expansion (Abramowitz & Stegun,
    Handbook of Mathematical Functions, formula 26.7.5) -- avoids a scipy
    dependency, which turned out to be unusable in this environment (a
    Windows Application Control policy blocks one of its compiled DLLs,
    confirmed this session). statistics.NormalDist is pure-stdlib and
    unaffected. Accurate to several decimal places for df >= ~10, which
    covers this project's realistic range (MIN_BUY_BUCKET_N=20 means
    df >= 19) -- more precision than a threshold/gating use needs."""
    z = NormalDist().inv_cdf(p)
    g1 = 1 / df
    g2 = g1 * g1
    g3 = g2 * g1
    g4 = g3 * g1
    z2, z3, z5, z7, z9 = z**2, z**3, z**5, z**7, z**9
    f1 = (z3 + z) / 4
    f2 = (5 * z5 + 16 * z3 + 3 * z) / 96
    f3 = (3 * z7 + 19 * z5 + 17 * z3 - 15 * z) / 384
    f4 = (79 * z9 + 776 * z7 + 1482 * z5 - 1920 * z3 - 945 * z) / 92160
    return z + f1 * g1 + f2 * g2 + f3 * g3 + f4 * g4


def buy_edge_lower_bound(returns, alpha=0.20):
    """wilson_lower_bound's counterpart for BUY-side (long option) edge --
    see conversation. A buy's payoff is the opposite shape from a sell's:
    expected to lose the premium (-100%) most of the time, with edge living
    in rare large winners. Win-rate/Wilson is the wrong lens for that
    (it's built for a bounded-upside proportion, not a right-skewed
    return), so gating buy-side candidates on wilson_low silently excludes
    real distributed edge (confirmed on real data: wallet jagged-wolf-eb's
    SOL buy-side record -- 13 trades, 5 real distinct winners from +20% to
    +1241% against 8 losses, net strongly positive -- has wilson_low
    8-19%, entirely excluded by the existing WEAK_EDGE_WILSON_BAR gate).

    But the naive fix (gate on raw mean return) is a worse mistake, not a
    better one: also confirmed on real data, wallet onyx-wolf-73's BTC
    call-buy bucket shows a +16,146% *average* return driven entirely by
    one $5->$6,347 trade, while actually losing money on 69% of its
    trades. A raw average can't tell "real distributed edge" from "one
    lucky outlier" apart.

    Returns (lcb, top1_gain_share, n):
      lcb: a conservative lower-confidence-bound on the true mean return,
        analogous to wilson_low -- higher is more trustworthy. Built from
        a Student-t interval with Johnson's (1978) skewness correction
        (t*_c = t_c + (g1/(6*sqrt(n)))*(1+2*t_c**2)), since this return
        data is heavily right-skewed (a wall of -100% losses plus a thin
        tail of large wins) and the *uncorrected* t-interval is
        anti-conservative under skew -- exactly the direction that would
        let onyx-wolf-73 slip through. sd==0 (every trade identical, e.g.
        all -100%) is handled by returning the mean directly rather than
        dividing by zero.
      top1_gain_share: fraction of total positive return contributed by
        the single best trade. An independent, assumption-free check the
        moment-based lcb alone can't fully replace -- a first-order
        (3rd-moment) skewness correction can still be mildly anti-
        conservative when kurtosis is also elevated (a mass point at
        -100% plus a fat right tail does exactly that). Confirmed on the
        two real wallets above: jagged-wolf-eb's is ~49% (edge spread
        across 5 real wins), onyx-wolf-73's is ~98% (one trade is nearly
        the entire story) -- a wide, unambiguous margin. A plain
        drop-the-best-trade-and-recheck approach was tried and rejected:
        it isn't normalized by anything, so no fixed drop-count classifies
        both wallets correctly (see conversation).
      n: sample size, passed through so callers can apply their own
        minimum (this function does not gate on sample size itself).

    Both numbers are provisional starting points from a design review, not
    yet calibrated against the full real population the way
    QUALIFY_WILSON_BAR was -- see scripts/build_trade_confidence.py."""
    n = len(returns)
    if n == 0:
        return None, None, 0

    arr = np.array(returns, dtype=float)
    mean = float(arr.mean())
    positive_sum = arr[arr > 0].sum()
    top1_gain_share = float(arr.max() / positive_sum) if positive_sum > 0 and arr.max() > 0 else 0.0

    if n < 2:
        return mean, top1_gain_share, n

    sd = float(arr.std(ddof=1))
    if sd == 0:
        return mean, top1_gain_share, n

    se = sd / math.sqrt(n)
    if n >= 3:
        # Population (biased) skewness -- g1 = m3 / m2^1.5 -- matching the
        # formula the design review's hand-worked examples used, not
        # scipy.stats.skew's small-sample-corrected variant.
        centered = arr - mean
        m2 = float((centered**2).mean())
        m3 = float((centered**3).mean())
        skew = m3 / (m2**1.5) if m2 > 0 else 0.0
    else:
        skew = 0.0
    t_c = _t_ppf(1 - alpha, df=n - 1)
    t_c_adj = t_c + (skew / (6 * math.sqrt(n))) * (1 + 2 * t_c**2)
    lcb = mean - t_c_adj * se
    return lcb, top1_gain_share, n


def _period_stats(returns):
    n = len(returns)
    wins = sum(1 for r in returns if r > 0)
    return {
        "n": n,
        "win_rate": wins / n,
        "wilson_low": wilson_lower_bound(wins, n),
        "total_return_pct": sum(returns),
        "median_return_pct": float(np.median(returns)),
    }


def qualify_wallet(returns_sorted):
    """returns_sorted: list of return_pct in chronological order (by
    resolved_ts) for one wallet's copy-eligible positions.

    Returns a dict with total_n, tier, qualified_at_n, pre-qualification
    stats, and forward (post-qualification) stats. tier is one of:
      insufficient_data   -- total_n < MIN_QUALIFY_N, can't evaluate yet
      does_not_qualify     -- enough data, but never cleared the bar
      qualified_but_faded  -- cleared the bar once, but the forward record
                              (n >= MIN_FORWARD_N) didn't hold up
      provisional          -- cleared the bar, forward record too thin to
                              judge yet (n < MIN_FORWARD_N)
      validated             -- cleared the bar, forward record holds up
      high_confidence        -- validated AND a large total + forward sample
    """
    total_n = len(returns_sorted)
    if total_n < MIN_QUALIFY_N:
        return {"total_n": total_n, "tier": "insufficient_data", "qualified_at_n": None,
                "pre": None, "forward": None}

    cum_wins = 0
    cum_return = 0.0
    qualify_idx = None
    for i, r in enumerate(returns_sorted, start=1):
        if r > 0:
            cum_wins += 1
        cum_return += r
        if i >= MIN_QUALIFY_N:
            wl = wilson_lower_bound(cum_wins, i)
            if wl >= QUALIFY_WILSON_BAR and cum_return > 0:
                qualify_idx = i
                break

    if qualify_idx is None:
        return {"total_n": total_n, "tier": "does_not_qualify", "qualified_at_n": None,
                "pre": None, "forward": None}

    pre = _period_stats(returns_sorted[:qualify_idx])
    forward_returns = returns_sorted[qualify_idx:]
    forward = _period_stats(forward_returns) if forward_returns else {"n": 0, "win_rate": None, "wilson_low": None, "total_return_pct": None, "median_return_pct": None}

    if forward["n"] < MIN_FORWARD_N:
        tier = "provisional"
    elif forward["wilson_low"] >= 0.50 and forward["total_return_pct"] > 0:
        tier = "high_confidence" if (total_n >= HIGH_CONFIDENCE_TOTAL_N and forward["n"] >= HIGH_CONFIDENCE_FORWARD_N) else "validated"
    else:
        tier = "qualified_but_faded"

    return {"total_n": total_n, "tier": tier, "qualified_at_n": qualify_idx, "pre": pre, "forward": forward}


def risk_discipline_score(tail_ratio, rfq_pct, sizing_cv):
    """0-100, unweighted average of three components, each independently
    grounded in something we already compute:
      - tail: full-history total return / |worst single position|, capped
        at 20 so one extreme wallet can't dominate the scale.
      - rfq: fraction of legs that were RFQ-matched (bilateral, pre-agreed
        price) -- 0% is ideal; see the RFQ-artifact bug found earlier this
        project (ember-harbor-e2), where a wallet's apparent edge turned
        out to be entirely a pricing tautology from trading against a
        near-exclusive counterparty.
      - sizing: coefficient of variation (std/mean) of notional_usd across
        this wallet's own trades -- lower means consistent sizing (the
        opposite of brass-basin-71's one oversized naked sale that erased
        months of otherwise-disciplined gains). CV >= 2.0 scores 0.
    Returns None if any input is None (not enough data to score)."""
    if tail_ratio is None or rfq_pct is None or sizing_cv is None:
        return None
    tail_component = min(max(tail_ratio, 0), 20) / 20 * 100
    rfq_component = (1 - rfq_pct) * 100
    sizing_component = max(0, 100 * (1 - sizing_cv / 2.0))
    return (tail_component + rfq_component + sizing_component) / 3
