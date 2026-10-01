# Undertow — Complete Strategy Reference

Generated 2026-08-22 directly from the live codebase (`config.py`, `scripts/*.py`,
`analysis/*.py`, `db/schema.sql`, `api/main.py`) — not from memory or the original
plan doc, since both code and design have moved substantially since `PROJECT_PLAN.md`
was written. Where something described there is superseded, this document says so.

**Core philosophy** (stated early in this project's life, still the operating
principle): *the wallet is signal, our system decides whether and how to express
it.* A wallet's track record is evidence that it has skill — not an instruction to
inherit its exact risk, sizing, or timing wholesale. Everything below is the
machinery that turns "this wallet looks good" into "here is what we actually do
about it," at every stage adding independent judgment rather than blind mirroring.

---

## 1. What this system is

A personal, unattended, **paper-trading** (simulated, no real money, no
credentials) system that:

1. Ingests every BTC/ETH options trade on Derive (a decentralized options
   exchange), wallet-by-wallet.
2. Continuously re-scores every wallet that has ever traded, using a no-look-ahead
   backtest of "would copying this wallet's opens, at realistic copier prices,
   have been profitable."
3. Maintains a curated, auto-updating watchlist of wallets whose track record
   clears a real statistical bar.
4. Mirrors each watchlisted wallet's *first-ever* trade in any new instrument as
   a simulated paper position, priced the same way the backtest was validated.
5. Protects every mirrored naked short with a live, polling stop-loss.
6. Tracks the outcome of both sides — our paper trade and the source wallet's own
   real position — so the two can be compared honestly.

Nothing here places a real order. `EXECUTION_ENABLED` is `false` and no code path
reads the `DERIVE_WALLET`/`DERIVE_SESSION_KEY`/`DERIVE_SUBACCOUNT_ID` credentials
yet — real execution ("Phase 4") is a deliberately deferred, separately-gated
future step (see §10).

---

## 2. Data pipeline

### 2.1 Source

**Derive (formerly Lyra)**, a decentralized options exchange, is the sole data
source for both BTC and ETH. (Deribit was evaluated and dropped — see §2.4 — but
its client/poller are left in the repo, unused, in case a higher-volume BTC
signal is wanted later.)

- `clients/derive_client.py` talks to `https://api.lyra.finance` via two public
  REST endpoints:
  - `POST /public/get_trade_history` — historical + incremental trade legs,
    paginated (1000/page), 5-retry exponential backoff on transient network
    errors.
  - `POST /public/get_ticker` — **live-only** snapshot (mark price, index price,
    best bid/ask, Greeks). No historical equivalent exists for this endpoint —
    confirmed empirically — which is why anything needing a *past* mark price
    (e.g. the historical stop-loss backtest) has to approximate with intrinsic
    value instead of a real quote.
- Rate limit: Derive enforces 10 TPS with a 5x burst per IP. This project uses a
  conservative fixed `0.15s` delay between paginated requests
  (`DERIVE_REQUEST_DELAY_SECONDS`).

### 2.2 What's pulled per trade leg

Each `options_events` row stores: `source`, `asset` (BTC/ETH), parsed
`instrument`/`strike`/`expiry`/`option_type` (parsed from the instrument name,
e.g. `ETH-20260828-3400-C`), `side` (buy/sell), `size`, `price` (premium),
`notional_usd` (`size × index_price_usd` at trade time), `index_price_usd`,
`wallet_address`, `tx_hash`, `source_trade_id`, `timestamp` (ms), `mark_price` at
execution, `rfq_id` (non-null if bilaterally block-negotiated), `tx_status`,
`trade_fee`, `liquidity_role` (maker/taker), `realized_pnl`, and the full raw
JSON response (kept for anything not worth its own column — notably, Derive
never exposes Greeks on historical trade legs, only on the live ticker).

### 2.3 Cadence

| Job | Cadence | What it does |
|---|---|---|
| `scripts/backfill_derive.py` | one-time, manual | Full historical pull, `2025-01-20` (US presidential inauguration — the analysis start date, not an arbitrary lookback window) → now, both currencies. |
| `scripts/live_poll.py`'s `ingest()` | every 15 min, as part of the watchlist agent tick | Incremental pull since `ingest_state.last_ts`, same upsert path as the backfill so there's exactly one insert/conflict codepath, not two that could drift. |

The live agent (`scripts/run_watchlist_agent.py`) is **not** a resident/long-lived
process — it's a stateless script invoked on a schedule (Windows Task Scheduler),
because every piece of state it needs between runs (`ingest_state.last_ts`,
`watchlist.last_checked_ts`, `paper_trades.fill_status`) is already persisted in
SQLite. A resident process would only add crash/reboot supervision burden for no
benefit.

### 2.4 Deribit (dropped)

Originally scoped as a supplementary high-volume BTC signal. Dropped once Derive
was confirmed to cover BTC with full historical + wallet-level data — Deribit
added raw volume but no wallet identity and (confirmed empirically) no real
historical backfill path (its public trade endpoint only returns ~24-48h
regardless of requested range). Code kept, unused.

### 2.5 On-chain wallet activity (Blockscout)

`clients/blockscout_client.py` + the `wallet_activity` table cover Phase 6 of the
original plan: once a wallet is interesting, pull its broader on-chain history
(deposits, withdrawals, transfers, swaps, staking, and raw options-trade
mechanics) from Derive Chain's Blockscout explorer. `method` retains the raw
contract method name (e.g. `sendBatch`, `handleOps` — ERC-4337 smart-account
internals) so the activity-type classification stays checkable against ground
truth rather than a guess.

### 2.6 Storage

Single local SQLite database (`data/undertow.db`). No hosted DB — deliberate,
v1-appropriate given the scale (a few hundred wallets, low hundreds of
thousands of trade legs).

---

## 3. Wallet rating & selection algorithm

This is the heart of "which wallets are worth following," and it happens in
layers — each one asking a progressively more specific question.

### 3.1 Layer 1 — Copy-trade backtest (`scripts/backtest_copy_trades.py`)

Question: *if a copier had mirrored every position this wallet opened, entering
not at the wallet's own fill but at a realistic copier's price, would it have
been profitable?* This is deliberately a different (harder) question than "is
this wallet's own win rate good" — a wallet can have great fills and still be a
bad copy target if its edge is priced/timing that a follower can't realistically
get.

**Entry/exit pricing**: the *next trade in that same instrument, by any OTHER
wallet*, at or after the reference timestamp, within a **24-hour staleness
window** (`COPY_STALENESS_WINDOW_MS`). If nothing prints within 24h, the position
is excluded as "not realistically copyable" — never priced stale.

**RFQ-artifact exclusion**: a print sharing an `rfq_id` the wallet itself already
used for that instrument is excluded from being a valid "next print." RFQ
(request-for-quote) trades are one bilateral deal split into two DB rows (maker +
taker) at an identical pre-agreed price, arriving seconds apart — without this
exclusion, "the next independent market print" is very often just the guaranteed
other half of the wallet's own trade (confirmed empirically: one wallet's "next
print" was its RFQ counterparty 6.7 seconds later at the identical price, for 298
of 304 trades). ~19% of all trade legs are RFQ-matched, so this isn't a rare edge
case — it was a real, previously undetected bug (`ember-harbor-e2`'s apparent
edge turned out to be entirely this pricing tautology) before the exclusion was
added.

**Outcome resolution**, in priority order:
1. Position closed by the wallet's own offsetting trade → copy exit price is the
   next other-wallet print after that close.
2. Never closed, but the instrument has since expired → exit priced via the same
   settlement estimate used everywhere in this project: nearest available
   `index_price_usd` (any wallet, same asset) to the expiry timestamp, converted
   to intrinsic value (`analysis/pnl.py`).
3. Still open, not expired → excluded, no outcome yet.

**Returns** are return-on-premium, the standard options convention: buy =
`(exit − entry) / entry`; sell = `(entry − exit) / entry`. Fees are not modeled
(a copier's fee schedule would differ from the source wallet's anyway). Ranking
uses the **95% Wilson score lower bound** on win rate, not raw win rate — this
penalizes small samples instead of trusting a number a handful of lucky trades
could produce by chance.

### 3.2 Layer 2 — No-look-ahead qualification (`analysis/qualification.py`)

This is the actual gate that decides if a wallet is followable, and it fixes two
real flaws in an earlier fixed-split design (kept in the schema as
`walkforward_backtest`/`copy_candidates`, superseded, nothing computes into them
anymore):

1. A wallet whose entire history starts after a fixed global cutoff had zero
   data on one side and could never be evaluated, no matter how good
   (`drift-vault-68` was the case that surfaced this).
2. Comparing two arbitrary fixed halves doesn't ask "would we actually have
   selected this wallet at the time" — it can show apparent persistence with no
   real point-in-time decision behind it (look-ahead bias).

**The walk**: a wallet's copy-eligible positions, in chronological order.
Cumulative win rate (Wilson lower bound) and cumulative return are tracked as
each new position resolves. The **first point** where, using *only* data
available up to that point:
- at least **15 positions** (`MIN_QUALIFY_N`) have resolved, and
- Wilson lower bound **≥ 60%** (`QUALIFY_WILSON_BAR`), and
- cumulative return is positive

...is the wallet's `qualified_at_n` — the position at which a real, disciplined
copier would have started following it. Everything **after** that point is the
`forward_*` record: a genuine out-of-sample answer to "we found this wallet at
that moment and started copying it — what actually happened next."

60% Wilson lower bound is not a round guess — it's calibrated against the real
population: among wallets with 15+ copy-eligible positions, a 60% bound is the
**top ~7%** (median is 22.9%, p90 is 52.4%).

**Tiers** (`wallet_qualification.tier`):

| Tier | Meaning |
|---|---|
| `insufficient_data` | fewer than 15 total positions — can't evaluate yet |
| `does_not_qualify` | enough data, never cleared the 60% bar |
| `qualified_but_faded` | cleared the bar once, but the forward record (≥15 positions) didn't hold up |
| `provisional` | cleared the bar, forward record still too thin (<15) to judge |
| `validated` | cleared the bar, forward record holds (Wilson ≥ 50% and positive return) |
| `high_confidence` | validated, AND total_n ≥ 75 AND forward_n ≥ 30 |

### 3.3 Layer 3 — Risk discipline score (`analysis/qualification.py`)

A single 0-100 synthesized score, deliberately the *only* invented composite
metric in v1 (everything else shown is a real underlying number — n, Wilson
bounds, returns — rather than compressed into more unvalidated scores). Simple
unweighted average of three components, each grounded in something already
computed:

- **Tail ratio** — full-history total return ÷ |worst single position|, capped
  at 20 (so one extreme wallet can't dominate the scale).
- **RFQ cleanliness** — `(1 − rfq_pct) × 100`. Lower RFQ share is better; see the
  `ember-harbor-e2` bug above.
- **Sizing consistency** — coefficient of variation (std/mean) of the wallet's
  own notional_usd across trades; lower is more disciplined. The opposite of
  `brass-basin-71`'s single oversized naked sale that erased months of otherwise
  disciplined gains. CV ≥ 2.0 scores 0.

Returns `None` if any input is missing (not enough data to score).

### 3.4 Layer 4 — Per-bucket edge profile (`scripts/build_trade_confidence.py`)

A wallet's edge is usually concentrated in a specific *kind* of trade, not
uniform across everything it does — a wallet proven at selling ETH puts isn't
necessarily proven at buying BTC calls, even with a great overall win rate.
`wallet_edge_profile` re-slices the same validated copy-trade positions by
`(wallet, asset, option_type, entry_side)`, keeping only buckets with ≥5
positions (`MIN_BUCKET_N`).

**Currently used only as a negative gate, deliberately** — not to boost or rank
anything (no evidence yet that it's predictive beyond screening out the
proven-bad end; filtering more aggressively now would slow down collecting the
outcome data needed to check). See §5.2.

### 3.5 Bot filtering (`scripts/build_leaderboard.py`)

Heuristic, not inferred identity: `legs_per_position > 8 OR legs_per_day > 15`
marks a wallet `is_likely_bot`. Genuine directional traders in this dataset
cluster well under both thresholds; known market-maker wallets run into the
hundreds. Bot wallets are excluded from ever qualifying onto the watchlist,
regardless of returns.

---

## 4. Watchlist maintenance (`scripts/sync_watchlist.py`)

The watchlist is a **frozen snapshot table**, deliberately *not* a live join to
`wallet_qualification` at read time — if a wallet's tier degrades while we have
an open paper position against it, we still want to know we were tracking it and
why, not have it silently vanish from a query.

**Auto-add** (two paths):
1. `tier IN ('validated', 'high_confidence')` → added regardless of recent
   activity. The historical evidence is the point; several watchlist wallets go
   quiet for months and stay on.
2. `tier == 'provisional'` AND traded within the last **30 days**
   (`ACTIVE_WITHIN_DAYS`) → added. This is the path that catches a wallet like
   `drift-vault-68` automatically, without a human spotting it first.

`is_likely_bot` wallets are never eligible either way.

**Auto-remove**: any currently-active watchlist wallet whose *current*
(recomputed) tier has dropped to `qualified_but_faded` or `does_not_qualify` is
deactivated (`is_active=0`, with a logged reason — forward Wilson/return at time
of removal). Both tiers mean the same thing from a copier's standpoint ("not
currently worth following"); they only differ in whether the wallet ever cleared
the bar in the first place. This removal logic was added after `iron-grove-22`
sat on the watchlist for months after fading — it had been added under an older,
now-superseded system that only ever added, never removed.

Wallets are **never deleted**, only deactivated with a reason, for auditability.

---

## 5. Live mirroring — how a trade actually gets copied

`scripts/run_watchlist_agent.py`, one tick = one full pass, run every 15 minutes.

### 5.1 Mirror rule

**Only the first-ever trade for a given (wallet, instrument) pair qualifies.**
This matches exactly what the backtest validated — it never extrapolates beyond
backtested evidence into mirroring incremental adds, reduces, or closes.

### 5.2 Classification (`classify_candidate`)

Every newly-detected first-ever-trade candidate is looked up against
`wallet_edge_profile` for its exact `(wallet, asset, option_type, side)` bucket:

- If that bucket has **n ≥ 5** history AND **Wilson lower bound < 50%** (worse
  than a coin flip, in something the wallet has actually done enough of to
  judge) → immediately `skipped_weak_edge`. Never reaches pricing.
- A combo with **no bucket yet** (untested, not proven-bad) still passes through
  — absence of evidence isn't evidence of badness, and gating on it would mean a
  wallet could never have its edge in something new discovered.
- Otherwise → `pending_entry`, whether buy **or sell**. Naked shorts are
  mirrored, not skipped (see §6 for how they're protected).

### 5.3 Entry pricing (`find_copy_entry_price` / `advance_pending_entries`)

Live equivalent of the backtest's pricing logic: the first trade in that
instrument by a *different* wallet, at or after the reference timestamp, not
sharing an RFQ id the source wallet used for that instrument, within the same
24-hour staleness window. Retried every tick while `pending_entry`. If nothing
valid prints within the window → `skipped_stale`.

`entry_ts` is set to when that copy price **actually existed** in the market,
not the wall-clock time the agent happened to notice it — these can differ
meaningfully when running against a backlog or a slow tick.

---

## 6. Risk management — the live stop-loss engine

`check_live_stops()`, run every tick, immediately after entry pricing.

### 6.1 Mechanism

Applies **only** to open naked shorts (`side='sell'`) that haven't reached their
own expiry yet. For efficiency, polls `/public/get_ticker` **once per distinct
instrument per tick** (not once per position — several open positions can share
an instrument).

Every tick, for every open naked short in that instrument:
1. A `paper_trade_marks` snapshot row is written: `mark_price`, `index_price`,
   `unrealized_return_pct`, `best_bid_price`/`best_ask_price`, and the full
   Greeks set (`delta`, `gamma`, `theta`, `vega`, `iv`, `rho`) straight off the
   same ticker response — no extra API calls. **Collection only** right now;
   nothing reads these to gate a decision yet (see §10).
2. If `mark_price ≥ STOP_MULTIPLE × entry_price` (**1.10×** the premium
   originally collected), the position is force-closed: `fill_status =
   'stopped_out'`, `exit_price = mark_price`, `return_pct` computed off that
   live mark.

A per-instrument ticker fetch failure is logged and skipped for that tick — not
fatal, retried next run. A bought option's loss is already capped at 100% of
premium paid, so buys are never snapshotted or stopped.

### 6.2 Why 1.10×

Settled via a full sensitivity sweep from 1.01× to 5.0× (`scripts/
analyze_stop_sensitivity.py`, `scripts/analyze_stop_wallet_correlation.py`)
across two populations: 8 historically-blown-up wallets (`cedar-ridge-e9`,
`iron-grove-22`, `cedar-basin-72`, `north-quarry-44`, `copper-current-1e`,
`drift-grove-08`, `glacial-thicket-ce`, `faded-summit-12` — every one of them had
a good win rate wiped out by naked shorts held to expiry with zero protection),
and the live watchlist as of the sweep.

The swept curve never found a true interior optimum — total return kept
improving monotonically all the way down to 1.01×. So 1.10× isn't "the
optimum," it's a deliberate step back from the tightest tested levels
(1.01×-1.05×), which showed a real, measurable **entry-confirmation artifact**:
stops firing on the very first available price print after entry — i.e.
detecting an already-stale copy-entry, not genuine mid-life deterioration. That
artifact was directly tested by excluding every first-print-triggered stop and
recomputing: it barely moved the numbers (under 1%, net-positive on the
watchlist), confirming the tight-stop advantage is mostly real — but 1.10× was
chosen over the untested edge as the more defensible point on an already-flat
part of the curve.

Confirmed to beat both alternatives by a wide margin on both populations: doing
nothing (naked, unprotected, held to expiry) and skipping naked shorts entirely
(the old default, before this engine existed).

### 6.3 Known live limitation (found in production, not theoretical)

The stop only checks once per 15-minute tick. On fast-moving, cheap, or
near-expiry contracts, the mark price can gap several multiples past 1.10× in a
single window before the next tick catches it — meaning some `stopped_out`
trades realize losses well past the intended ~10% cap (one observed case: entry
`$2.80` → exit `$28.20`, a 10× move, not 1.10×). This is a **polling-cadence
execution artifact**, not a flaw in the 1.10× threshold itself — the historical
backtest that validated 1.10× also can't fully capture this, since it has no
real intra-life mark-price data to test against (see §6.4). In real (Phase 4)
execution, this would be mitigated by a resting stop order on the exchange
itself, which reacts intraday instead of waiting for the next poll.

### 6.4 Historical-backtest caveat

Derive's ticker only exposes the *current* live price — there is no historical
mark-price/Greeks endpoint. `scripts/backtest_stop_loss.py` and the sensitivity
sweep approximate a historical stop check using the same expiry-settlement proxy
used everywhere else (nearest index price + intrinsic value), applied at
intermediate points in a position's life instead of only at expiry. Intrinsic
value ignores time value, so this **systematically understates** how early a
real stop would trigger — a short call's real market price already reflects
extrinsic value before it's even in the money. The backtest is a conservative,
best-available proxy, not a claim that live polling behaves identically.

---

## 7. Settlement & resolution

### 7.1 Normal expiry (`scripts/resolve_paper_trades.py`)

**v1 simplification, stated deliberately, not an oversight**: every paper
position is held to expiry rather than mirroring the source wallet's own
subsequent closing trades (except naked shorts, which can now also close early
via the stop). Whether to follow the wallet's own exits too is a real, deferred
design question pending enough resolved-trade data to reason about.

At expiry: settlement price = nearest available `index_price_usd` (any wallet,
same asset) to the expiry timestamp, standing in for the real settlement price
Derive's trade API doesn't expose. Exit value = intrinsic value at that spot.
`pnl_is_estimated = 1`.

### 7.2 Source wallet's own outcome (`check_wallet_outcomes`)

Independently, every tick, for every paper trade whose *wallet-side* outcome
isn't known yet: determines what the **source wallet's own position** (not our
copy) actually closed at, regardless of our own `fill_status` — so this fills in
even for `skipped`/`pending` rows, since the wallet's position happened
regardless of whether we managed to mirror it.

Two resolution paths, mirroring `compute_wallet_positions` in `api/main.py`
exactly:
1. Wallet closed it themselves via a real offsetting trade → `wallet_exit_price`
   is the volume-weighted price of those closing legs, `wallet_pnl_is_estimated
   = 0`.
2. Never closed, instrument has since expired → same nearest-index-price +
   intrinsic-value estimate, `wallet_pnl_is_estimated = 1`.
3. Still open, unexpired → left `NULL`, rechecked next tick.

This is what powers the dashboard's "Us vs. the source wallets" comparison — our
side is always fully known (`completed = resolved + stopped_out`); the wallet
side is necessarily a smaller, growing subset, since a stop-loss resolves us
*faster* than an unprotected position resolves for the wallet. The two are
shown as different sample sizes on purpose, not forced to match.

---

## 8. Position sizing & income reporting

`paper_trades` carries **no real dollar size** — trades are copied at the
source wallet's own contract size, which ranges from ~$5 to ~$436 in raw
premium across the current watchlist, not comparable to sum directly. Every
trade's `return_pct` is instead normalized against a flat hypothetical stake for
reporting:

- **$10/trade** (`HYPOTHETICAL_STAKE_USD`) — matches the real Phase 4 execution
  cap already decided in advance (§10).
- **$5/trade** (`HYPOTHETICAL_STAKE_USD_SMALL`) — added as a second lens once
  research showed the median real minimum tradeable size across the *current*
  watchlist is ~$2.50, with ~67% of trades feasible at ≤$5 and ~82% at ≤$10 —
  meaningfully more favorable than an earlier, now-outdated recollection from
  a smaller/older watchlist. Not a decision to actually trade at $5; a preview
  of what a small live budget's economics would look like.

Income = `return_pct × stake`, shown both overall and per-source-wallet.

---

## 9. Anomaly detection (the original, still-dormant thread)

Separate subsystem from the copy-trading pipeline, feeding the **Signals** tab —
this was the project's original v1 hypothesis (does unusual options activity
precede price-moving news?), deprioritized (not removed) once the wallet-copying
thread proved more immediately productive (`PROJECT_PLAN.md` §9).

- **Large trade** (`flag_large_trades`): per-trade size z-score of
  `log(notional_usd)`, baselined per day-of-week over the whole backfilled
  corpus (log-transformed since trade size is heavy-tailed). Threshold **z ≥
  3.0** (`TRADE_SIZE_ZSCORE_THRESHOLD`).
- **Wallet jump** (`flag_wallet_jumps`): a wallet trading **≥ 8×**
  (`WALLET_JUMP_MULTIPLIER`) its own expanding walk-forward average
  log-notional (only trades strictly *before* the one evaluated — no
  look-ahead), gated on ≥15 prior trades, ≥$200k notional, and low prior
  variance (std-log ≤ 1.0, so an erratic wallet doesn't constantly "jump" on
  its own noise). This single variance constraint cut the flag rate from 1.36%
  to 0.02% in testing.
- **Daily signals**: volume spikes (z ≥ 2.5) and call/put skew shifts (|z| ≥
  2.5), both baselined per day-of-week (weekends run ~40-45% below weekday
  volume — a flat baseline would misfire on every Friday-vs-Sunday swing).

These populate `options_events.is_flagged`/`flag_reason`/`anomaly_score` and the
`daily_signals` table, driving the Signals tab's price/expiry charting around a
flagged event.

---

## 10. Explicitly not built yet

Stated plainly so nothing above is mistaken for further along than it is:

- **Real order execution ("Phase 4")** — `.env`-based Derive testnet
  credentials, a liquidity smoke-test, `mode='testnet_order'` alongside the
  existing `mode='simulated'`. `EXECUTION_ENABLED` defaults `false`; nothing
  reads the credential env vars yet. Pre-decided sizing rule for when it is
  built: cap real orders at **$10 of premium**, and if the instrument's
  exchange-enforced minimum contract size costs more than that, skip mirroring
  the trade (a `skipped_min_size`-style status) rather than sizing up past the
  cap.
- **Greeks-driven decisions** — collected every tick (§6.1) but not read by any
  gate, sizing, or exit logic yet. A deliberate follow-up once there's enough
  accumulated history to check against.
- **Capital-constrained / small-portfolio selection logic** — discussed
  conversationally (lowering copy rate below "every trade," favoring
  short-dated expiries for faster turnover, an explicit edge threshold to copy
  at) but not implemented. Forward-thinking only.
- **Per-wallet custom stop thresholds** — `stop_wallet_correlation` checks
  whether being hurt by the global 1.10× stop correlates with anything already
  known about a wallet (risk score, RFQ%, sizing CV), but nothing acts on it. A
  single global multiple applies to everyone, deliberately, to avoid overfitting
  a per-wallet threshold at the sample sizes most watchlist wallets have.
- **News/catalyst correlation** — the original v1 hypothesis (Phases 1-7 of
  `PROJECT_PLAN.md`): manual event annotation, Trump-post correlation, price/
  expiry charting around flagged events. Code and data intact, dormant, not
  removed.

---

## 11. Scheduled jobs — full cadence summary

All three run as independent Windows Scheduled Tasks, durable across reboots,
independent of any Claude session:

| Task | Cadence | Skip-if-quiet? | What it does |
|---|---|---|---|
| `UndertowWatchlistAgent` | every 15 min | no — always runs | Live ingest → alias new wallets → detect new positions → price pending entries → **poll live stops** → resolve expired → check wallet-own outcomes → log an `agent_runs` row. |
| `UndertowRefreshQualification` | hourly | **yes** — skips the ~100s full pass if no active watchlist wallet has a trade newer than its last qualification | Full-universe `wallet_qualification` rebuild (all wallets, not just watchlist) → `sync_watchlist` (auto add/remove). |
| `UndertowRefreshTradeConfidence` | hourly (offset 30 min from the qualification task) | **yes**, same pattern | Rebuilds `wallet_edge_profile` (the per-bucket weak-edge gate data). |

The two hourly jobs deliberately run *separately* and *slower* than the 15-min
agent tick — stacking a ~100s+ full requalification onto every 15-min tick would
meaningfully eat its budget and risk exactly the kind of concurrent-load
"database is locked" crash this project hit once already. The skip-if-nothing-
changed pattern (`needs_refresh()`, comparing each active wallet's latest trade
timestamp against the ratings table's last `computed_at`) protects genuinely
quiet stretches (overnight, weekends) from paying that cost for nothing.

---

## 12. Dashboard & API surface

FastAPI backend (`api/main.py`, ~29 endpoints) + a React/Vite dashboard with five
tabs:

- **Signals** — flagged anomalous events, price/expiry charting (§9).
- **Wallets** — search, per-wallet detail, live portfolio, on-chain activity,
  qualification ratings.
- **Leaderboard** — raw win-rate ranking, copy-backtest results, qualification
  tiers.
- **Paper Trading** — the live mirror ledger: open/stopped/resolved positions,
  per-source-wallet breakdown, hypothetical income at $10 and $5 stakes, and the
  "Us vs. the source wallets" comparison (§7.2).
- **Live Feed** — `agent_runs` tick-by-tick history, so the pipeline's actual
  15-minute cadence is directly checkable, not just assumed.

---

## 13. Key constants at a glance

| Constant | Value | Purpose |
|---|---|---|
| `BACKFILL_START_ISO` | `2025-01-20` | Historical backfill start date |
| `COPY_STALENESS_WINDOW_MS` | 24h | Max wait for a valid copy price before excluding/skipping |
| `STOP_MULTIPLE` | 1.10 | Live + backtest naked-short stop-loss trigger |
| `HYPOTHETICAL_STAKE_USD` | $10 | Primary income-normalization stake (matches planned Phase 4 cap) |
| `HYPOTHETICAL_STAKE_USD_SMALL` | $5 | Secondary income-normalization stake |
| `MIN_QUALIFY_N` | 15 | Minimum positions before a wallet can qualify |
| `QUALIFY_WILSON_BAR` | 0.60 | Wilson lower bound required to qualify (top ~7% of 15+-position wallets) |
| `MIN_FORWARD_N` | 15 | Forward positions needed to call a qualification "validated" vs. merely "provisional" |
| `HIGH_CONFIDENCE_TOTAL_N` / `_FORWARD_N` | 75 / 30 | Thresholds for the top `high_confidence` tier |
| `MIN_BUCKET_N` | 5 | Minimum positions for a `wallet_edge_profile` bucket to count |
| `WEAK_EDGE_WILSON_BAR` | 0.50 | Bucket Wilson bound below which a trade is gated out |
| `ACTIVE_WITHIN_DAYS` | 30 | Recency window for auto-adding a `provisional`-tier wallet |
| `LEGS_PER_POSITION_BOT_THRESHOLD` / `LEGS_PER_DAY_BOT_THRESHOLD` | 8 / 15 | Bot-heuristic thresholds |
| `DERIVE_REQUEST_DELAY_SECONDS` | 0.15s | Delay between paginated Derive requests |
| `TRADE_SIZE_ZSCORE_THRESHOLD` | 3.0 | Large-trade anomaly threshold |
| `WALLET_JUMP_MULTIPLIER` / `_MIN_NOTIONAL_USD` / `_MIN_PRIOR_TRADES` / `_MAX_PRIOR_STD_LOG` | 8.0 / $200k / 15 / 1.0 | Wallet-jump anomaly gating |
| `VOLUME_ZSCORE_THRESHOLD` / `SKEW_ZSCORE_THRESHOLD` | 2.5 / 2.5 | Daily-signal anomaly thresholds |
