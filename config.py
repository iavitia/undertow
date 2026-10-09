import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")  # no-op if .env doesn't exist -- fine for Phase 0-3, no credentials needed

DB_PATH = BASE_DIR / "data" / "undertow.db"
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"

# Was "https://api.lyra.finance" (V2) -- confirmed live this session that
# domain didn't 530 like the testnet one did (clients/derive_execution_client.py's
# docstring), it just silently stopped receiving new trades around the
# same Oct 6 2026 V3 migration: querying it with a wide time range still
# returns real historical data, but nothing after ~2026-10-06T17:53Z, no
# matter how recent the query window. That silently starved the entire
# ingest pipeline (scripts/live_poll.py) for 3+ days -- every tick
# "succeeded" (0 trades is a valid, no-op return, not an error) while
# wallet discovery/qualification/candidate detection all quietly saw
# nothing new. V3's public mainnet endpoint confirmed live with current
# data for ETH/BTC/SOL.
DERIVE_BASE_URL = "https://api.derive.xyz/v3"
DERIBIT_BASE_URL = "https://www.deribit.com/api/v2"

# Phase 4 credentials, unset until that phase is built -- see .env.example.
# Never used for Phase 0-3 (live polling + simulated paper trading).
DERIVE_WALLET = os.environ.get("DERIVE_WALLET")
DERIVE_SESSION_KEY = os.environ.get("DERIVE_SESSION_KEY")
DERIVE_SUBACCOUNT_ID = int(os.environ["DERIVE_SUBACCOUNT_ID"]) if os.environ.get("DERIVE_SUBACCOUNT_ID") else None
# V3 split BTC/ETH and SOL into separate risk universes (Prime vs Alt --
# see conversation) that can't share a subaccount, so SOL gets its own
# with its own, financially-independent margin pool. DERIVE_SUBACCOUNT_ID
# above stays the Prime (BTC/ETH) one for backward compatibility with
# every other script that only ever traded those two.
DERIVE_SUBACCOUNT_ID_SOL = int(os.environ["DERIVE_SUBACCOUNT_ID_SOL"]) if os.environ.get("DERIVE_SUBACCOUNT_ID_SOL") else None
# Small-capital, short-expiry-only subaccount (see conversation) --
# follows the same signal as Prime/Alt in parallel, not instead of them,
# specifically to observe real margin/fee/scheduling-delay economics
# under tight capital. None until the wallet is actually created --
# scripts/run_live_agent.py's SUBACCOUNT_CONFIGS skips it entirely while
# unset, same pattern as the other two.
DERIVE_SUBACCOUNT_ID_FAST = int(os.environ["DERIVE_SUBACCOUNT_ID_FAST"]) if os.environ.get("DERIVE_SUBACCOUNT_ID_FAST") else None
# Must stay "SEPOLIA" for the lifetime of this project's paper-trading/testnet
# phase -- clients/derive_execution_client.py refuses to run against
# anything else. Never set this to a mainnet chain casually.
DERIVE_ETH_CHAIN = os.environ.get("DERIVE_ETH_CHAIN")
EXECUTION_ENABLED = os.environ.get("EXECUTION_ENABLED", "false").lower() == "true"

# Shared between scripts/backtest_copy_trades.py (historical validation) and
# scripts/run_watchlist_agent.py (live mirroring) so the two can't silently
# drift out of sync -- same bug class as the value-timeline/portfolio P&L
# split earlier in this project's history.
COPY_STALENESS_WINDOW_MS = 24 * 60 * 60 * 1000  # 24h -- beyond this, not realistically copyable

# Paper trades store return_pct (return on premium), not a dollar amount --
# there's no per-trade size/quantity tracked, and raw premiums aren't
# comparable across instruments (see PROJECT_PLAN.md's Phase 4 sizing
# decision: one wallet's trades ranged from $5 to $436 in premium). This is
# the same $10-per-trade cap already decided for real Phase 4 execution,
# reused here to express paper-trading results as a hypothetical, size-
# normalized dollar figure (return_pct * this) instead of summing raw
# premiums, which would be apples to oranges.
HYPOTHETICAL_STAKE_USD = 10

# Secondary comparison stake, alongside HYPOTHETICAL_STAKE_USD -- not a
# decision to actually use $5 instead of $10, just a second lens on the
# same results. Real minimum-order-cost research against the current
# watchlist (see conversation) found the median trade's minimum tradeable
# size is well under $10 (~$2.50), and floated sizing each real trade at
# its own instrument's minimum rather than a flat cap, to fit far more
# concurrent positions into a small live budget. Showing income at both
# stakes side by side makes that trade-off visible now, before any of it
# is actually built.
HYPOTHETICAL_STAKE_USD_SMALL = 5

# Naked-short stop-loss multiple: historical research constant only as of
# the live-loss-floor change below (see conversation) -- still used by
# scripts/backtest_stop_loss.py, scripts/analyze_stop_sensitivity.py, and
# scripts/analyze_stop_wallet_correlation.py to ask "would an early,
# poll-triggered exit have helped," but scripts/run_watchlist_agent.py no
# longer reads this to gate live behavior. Retired from live use after two
# days of real production data: 48 live stopped-out trades averaged -63%
# realized loss (vs. the intended -10% cap), because a 15-minute poll
# cadence can't catch a short-dated, high-gamma naked short's mark price
# before it gaps well past the threshold -- and of the subset where the
# source wallet's own unprotected outcome was already checkable, 13 of 13
# would have been wins if left alone. Settled via a full sensitivity sweep
# from 1.01x to 5.0x on both the historically-blown-up wallets and the
# current watchlist (scripts/analyze_stop_sensitivity.py, scripts/
# analyze_stop_wallet_correlation.py) -- the swept curve never found a true
# interior peak (total return kept improving monotonically all the way to
# 1.01x on both populations), so 1.10x isn't "the optimum," it's a
# deliberate step back from the tightest tested levels (1.01x-1.05x), which
# showed a real, measurable entry-confirmation artifact (stops firing on
# the very first available price print after entry -- detecting an
# already-stale copy-entry rather than genuine mid-life deterioration).
# That artifact was directly tested by excluding every first-print-
# triggered stop and recomputing: it barely moved the numbers (under 1%,
# net-positive on the watchlist), confirming the tight-stop advantage is
# real in the historical-proxy backtest -- but that proxy (intrinsic value
# at discrete historical prints, see scripts/backtest_stop_loss.py's
# docstring) never captured the live, IV-driven whipsaw a real poll
# actually experiences on short-dated contracts, which is what live
# production surfaced.
STOP_MULTIPLE = 1.10

# How close analysis.pnl.nearest_index_price's matched sample must be to an
# instrument's real expiry moment before a settlement computed from it gets
# permanently persisted (scripts/resolve_paper_trades.py,
# scripts/run_watchlist_agent.py's check_wallet_outcomes). See conversation:
# without this, a settlement running right at/after expiry -- before market-
# wide trade volume (which is what index-price samples ride along on) had
# caught up near that exact moment -- could lock in a price from hours away
# as if it were current, with no way to ever revisit it once written. 30 min
# is generous relative to the ~15 min agent tick -- a position just skips
# settlement and retries next tick until a genuinely close sample exists,
# same resilience already used for "no index price at all yet."
SETTLEMENT_FRESHNESS_TOLERANCE_MS = 30 * 60 * 1000

# Replaces the live stop-loss engine above: instead of trying to catch a
# naked short's deterioration mid-life via a polled mark price (unreliable
# on short-dated, high-gamma contracts -- see STOP_MULTIPLE's comment),
# every naked short is now held to expiry like every other paper trade, and
# its realized loss is simply floored at 100% of the premium collected --
# the same max-loss shape as a bought option (lose everything at risk,
# never more). Models the real-world equivalent of a margined position
# being liquidated the instant posted margin (the premium collected) is
# exhausted, rather than us trying to catch that exact moment ourselves.
# Applied in scripts/resolve_paper_trades.py at settlement, not live --
# short-dated naked shorts stay in play for fast turnover (see
# conversation: needed for eventually testing a small, $100-scale live
# account), they just can't realize worse than this floor.
NAKED_SHORT_LOSS_FLOOR_PCT = -1.0

# Derive public REST is rate limited per-IP at 10 TPS with 5x burst.
DERIVE_REQUEST_DELAY_SECONDS = 0.15

# Deribit's public trade endpoint only returns ~24-48h of history, so BTC has
# no backfill window via Deribit (see PROJECT_PLAN.md section 3). Derive has
# full trade-level history (with wallet data) back to 2024-01-11 for both
# ETH and BTC, confirmed empirically, so the Derive backfill covers both
# assets starting from a fixed date rather than "N days back".
#
# Extended from 2025-01-20 (an arbitrary anchor, not a real limit -- see
# conversation) after sampling real daily trade volume across 2024: Jan 2024
# was ~117 trades/day, ~16x thinner than where the old start date already
# began (~1934/day), and volume doesn't reach a comparable, reasonably
# mature level until around mid-2024. 2024-06-01 was picked as the new
# start to capture that additional ~7-8 months of genuinely useful history
# -- 72% of the wallet universe was stuck at insufficient_data (<15
# copy-eligible positions) under the old window -- while skipping the
# thinnest, least mature ramp-up months (2024-01 to ~2024-04), which would
# add permanent processing cost to every future batch job for comparatively
# little qualifying signal. A deliberate first step, not the final answer:
# the plan is to measure the actual effect on wallet qualification counts
# and batch-job runtime at this window before deciding whether to push
# further back (2024-03-01, or the full 2024-01-11 once/if the system is
# robust enough that the processing cost of the thin early months stops
# mattering).
BACKFILL_START_ISO = "2024-06-01"

# Anomaly detection thresholds (Phase 2). All baselines are per
# asset + day-of-week, computed statically over the whole backfilled corpus
# -- weekends run ~40-45% below weekday volume (confirmed in Phase 1 sanity
# check), so a flat baseline would misfire constantly. These are meant to be
# tuned per the plan's test plan (section 6.2): start here, check against a
# known volatile date and a known quiet date, adjust.
TRADE_SIZE_ZSCORE_THRESHOLD = 3.0   # single-trade size outlier (on log-notional)
VOLUME_ZSCORE_THRESHOLD = 2.5       # daily total notional outlier
SKEW_ZSCORE_THRESHOLD = 2.5         # daily call/put skew outlier

# Second detector: a wallet suddenly trading far bigger than its own history,
# rather than big relative to the market. Computed cross-asset per wallet
# (a wallet's "usual size" is about its own behavior, not siloed by asset)
# using only trades strictly before the one being evaluated -- an expanding
# walk-forward baseline, not the whole-corpus static baseline the size
# detector uses, since "usually quiet" is inherently about trade order.
WALLET_JUMP_MIN_PRIOR_TRADES = 15   # need real history, not a 5-trade noisy sample
WALLET_JUMP_MULTIPLIER = 8.0        # trade notional >= 8x wallet's own prior average
WALLET_JUMP_MIN_NOTIONAL_USD = 200_000  # filters out noise from dust-sized jumps
# Prior trades must also have been *consistent* (low variance), not just few --
# without this, a wallet with 15 erratic dust trades constantly "jumps" on
# ordinary variance. This single constraint cut the flag rate from 1.36% to
# 0.02% in testing at these thresholds -- it's the difference between
# "was steady, then jumped" and "we don't have enough data yet."
WALLET_JUMP_MAX_PRIOR_STD_LOG = 1.0

