# On-Chain & Options Anomaly Research Tool — Project Plan (v1)

## 1. Purpose & Hypothesis

**Purpose:** A personal research tool to investigate whether large, unusual
derivatives activity (options flow) and on-chain wallet activity in crypto
markets statistically precede price-moving news or announcements more often
than chance would predict.

**Origin:** This started from an observed pattern in equities options markets
— large, unexplained options activity followed by an announcement that
appeared to benefit that position. This project adapts that same
research question to crypto, where better data access (on-chain
transparency) makes it more tractable to study.

**Hypothesis being tested:** When unusually large or concentrated options
activity (centralized) or wallet-level options/transfer activity (on-chain)
occurs, does it correlate with subsequent price-moving events at a rate
higher than baseline/chance?

---

## 2. Scope

### In Scope (v1)

- **BTC and ETH on-chain options** via Derive (formerly Lyra) — trade-level
  data including wallet addresses, size, direction, strike, expiry, for both
  assets (confirmed 2026-08-16: Derive's history goes back to 2024-01-11 for
  both currencies, so this is the sole source for both, not ETH-only)
- Historical backfill to bootstrap a usable dataset immediately — Derive
  backfill runs from 2025-01-20 (US presidential inauguration) forward,
  chosen as the analysis start date rather than a fixed 6-12 month window
- Ongoing live polling to keep the dataset growing
- Manual event annotation (news/announcements logged by hand)
- Price correlation against flagged events
- Basic wallet activity lookup: once a wallet is flagged via an options
  trade, pull its broader on-chain history (other trades, transfers) for
  context

### Out of Scope (v1 — possible future phases)

- Equities/traditional options markets (separate project; different data
  access model)
- Automated news scraping/matching (manual logging first; automate only if
  the hypothesis shows promise)
- Wallet identity inference, deanonymization, or clustering of unlabeled
  addresses
- Real-time push notifications or alerting
- Any trade execution, brokerage/exchange account integration, or wallet
  signing permissions
- Coins/tokens beyond BTC and ETH

---

## 3. Data Sources & Access

| Source                                                     | Data Provided                                                                               | Access Model                         | Est. Cost |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------ | --------- |
| Derive (Lyra) API                                          | On-chain BTC + ETH options trades incl. wallet address, size, direction, strike, expiry, back to 2024-01-11 | Public API, free     | $0        |
| Etherscan / Optimism (Arbiscan-equivalent for Derive's L2) | Full transaction history for any flagged wallet                                             | Free tier, rate-limited (~5 req/sec) | $0        |
| CoinGecko API                                              | BTC/ETH price history for correlation                                                       | Free tier                            | $0        |
| News/announcements                                         | Manual logging in v1                                                                        | N/A (human-reviewed)                 | $0        |

**Dropped for now: Deribit API.** Originally scoped as a supplementary BTC
live-poll signal (Deribit's raw trade volume is much higher than Derive's
BTC market). Decided 2026-08-16 to drop it once Derive was confirmed to
cover BTC with full historical + wallet-level data — Deribit added volume
but no wallet data and no backfill, so it stopped pulling its weight once
Derive covered both assets. `clients/deribit_client.py` and
`scripts/poll_deribit.py` are left in place, unused, in case the
higher-volume signal is wanted later.

**Estimated total monthly cost for v1: $0–20** (hosting only, e.g. a small VM
or running locally/on a home machine).

**Rate limit strategy:** Historical backfill runs as a one-time batched script
with delays between calls, not a live loop. Live polling limited to BTC/ETH
options data only (not full-chain monitoring) on a 15–30 minute interval,
which comfortably stays within all free-tier limits given the narrow scope.

**Known limitation — Deribit historical trade data:** Confirmed empirically
(2026-08-16) that Deribit's public `get_last_trades_by_currency_and_time`
endpoint only returns trades from roughly the last 24–48 hours, regardless of
the `start_timestamp` requested — there is no free historical backfill path
for BTC trade-level/volume/OI data via Deribit's public API. Free third-party
alternatives checked (CryptoDataDownload) only offer historical DVOL
(volatility index), not volume/OI/trade data, for free. Decision: BTC skips
historical backfill in v1 and starts from live polling only (see Phase 1
below); ETH/Derive backfill is unaffected — Derive's `get_trade_history`
retains full trade-level history back to at least Jan 2024.

---

## 4. Data Model

```
options_events (
  id, source (deribit/derive), asset (BTC/ETH), instrument,
  strike, expiry, direction (call/put), size, notional_usd,
  timestamp, is_flagged, flag_reason
)

wallets (
  id, address, chain, first_seen, label (nullable — only if
  already publicly known, never inferred)
)

wallet_activity (
  id, wallet_id, tx_hash, timestamp, activity_type
  (options_trade/transfer/swap), asset, amount, usd_value,
  related_options_event_id (nullable)
)

price_snapshots (
  id, asset, timestamp, price_usd, source
)

annotations (
  id, related_event_id (options_events or wallet_activity),
  note, related_announcement_date, related_announcement_summary,
  outcome_direction (aligned/not aligned/unclear), added_at
)
```

---

## 5. Build Phases

Ordered so that each phase produces something useful using only data already
in hand, before adding any dependency on a new source. News lookup in
particular is pulled forward as a lightweight helper available whenever it's
useful, rather than a gated "phase" — see 5.x below.

**Phase 1 — Backfill**

- BTC + ETH / Derive: pull full trade-level history (wallet address, size,
  direction, strike, expiry) via `get_trade_history`, from 2025-01-20
  (inauguration) forward, for both currencies — this is the sole dataset
  for both assets, wallet-level included (Deribit dropped, see §3)
- Load into local SQLite DB per schema above
- Sanity-check data completeness and spot-check a few known volatile periods

**Phase 2 — Anomaly Detection Logic**

- Define initial thresholds: volume/OI ratio, size relative to rolling
  average, put/call skew shifts
- Run detection against backfilled data, generate a first list of flagged
  historical events

**Phase 3 — Price/Expiry Charting (no news dependency)**

- For each flagged event, chart the position's strike/expiry against actual
  realized price action through expiry (or through a fixed review window if
  still open)
- This works with data already on hand (options data + CoinGecko price) —
  no news source needed yet
- Goal: get a first read on which flagged events look "interesting" purely
  from price action — e.g. far-OTM calls opened well ahead of expiry that
  the price actually moved toward — before spending any time on news lookup
- This naturally filters the list down to a smaller set worth digging into
  further, rather than manually reviewing news for every flagged event

**Phase 4 — Manual Review, with News Lookup as a Helper**

- For the subset of events that look interesting after price/expiry
  charting, manually review context
- News lookup utility: given a date, pull headlines from a general
  RSS/news source (e.g. a crypto news aggregator RSS feed, or a scoped
  Google News RSS query) for that window — a convenience function to save
  manual searching, not an automated matcher. No NLP/correlation logic —
  you read and judge relevance yourself
- Annotate findings per the schema (`annotations` table)

**Phase 5 — Live Polling**

- Same detection + charting logic, run on a schedule (15–30 min) going
  forward
- Same DB, same schema — no separate live-only pipeline
- Manual review continues the same way as Phase 4

**Phase 6 — Wallet-Level Follow-Through (Derive/on-chain only)**

- Applies to both BTC and ETH now that both are sourced from Derive — not
  ETH-only as originally scoped
- When a Derive options trade is flagged and clears the price/expiry filter,
  pull the associated wallet's broader transaction history via
  Etherscan/L2 explorer
- Store in `wallet_activity`, linked back to the triggering event
- No identity inference — just a fuller activity record for that address
- This is a second useful output regardless of whether any given event
  turns out to correlate with news: it builds a shortlist of wallets with
  unusual activity patterns worth exploring on their own merits

**Phase 7 — Review & Decide on Scope Expansion**

- After a few weeks of live data + backfill, review per the success
  criteria in Section 9 (a useful shortlist either way, not a strict
  pass/fail on the news-correlation hypothesis)
- Decide whether to expand (more assets, deeper news automation,
  statistical threshold tuning) or keep the tool as a lightweight,
  manually-reviewed discovery aid

---

## 6. Test / Research Plan

This section defines how we'll know if the tool and the hypothesis are
actually working — modeled loosely on a QA test plan, adapted for a research
tool.

### 6.1 Data Integrity Tests

- Backfill completeness: spot-check known high-volatility dates (e.g. a past
  major BTC price move) and confirm the corresponding options volume spike
  appears in the pulled data
- Confirm wallet activity pulled for a sample flagged event matches what's
  visible directly on a block explorer (manual cross-check)
- Confirm price snapshots align with known historical price points for a
  handful of spot-check dates

### 6.2 Detection Logic Tests

- Run the anomaly threshold logic against a known historical event (a date
  where a genuine large/unusual move is publicly documented) and confirm it
  gets flagged — a basic "does the detector detect the obvious case" check
- Run against a quiet/uneventful period and confirm it does NOT flag normal
  activity (false-positive check)
- Tune thresholds iteratively based on these two checks before trusting
  output on ambiguous periods

### 6.3 Correlation / Hypothesis Evaluation

- For each flagged + annotated event, record: was there a related
  announcement/news event within the review window (define window, e.g. 72
  hours)? Did price move in the direction the flagged position implied?
- Track a running "hit rate": of all flagged events, what percentage had a
  plausible follow-up event vs. no discernible follow-up
- Compare this hit rate against a rough baseline (e.g. how often ANY
  announcement happens in a random 72-hour window) to sanity-check whether
  the correlation is meaningfully above chance, not just a plausible-sounding
  pattern-match after the fact
- Explicitly track false positives — flagged events that led nowhere — with
  equal rigor to tracking hits, to avoid only remembering the interesting
  cases

### 6.4 Operational Tests

- Confirm live polling runs unattended for at least 2 consecutive weeks
  without manual intervention or crashes
- Confirm API rate limits are never exceeded (log and monitor call counts)
- Confirm the manual annotation workflow is quick enough to actually keep up
  with (if it takes too long per event, it won't get done — flag this early
  and simplify if needed)

### 6.5 Exit / Decision Criteria

At the end of the review period (suggest 4–6 weeks of combined backfill +
live data):

- **Continue/expand** if: flagged events show a hit rate meaningfully above
  a naive baseline, and annotation has been sustainable
- **Simplify or pause** if: too much noise (mostly false positives), or
  annotation burden is unsustainable, or no meaningful pattern emerges
- **Pivot** if: one data source (BTC aggregate vs ETH wallet-level) is
  clearly more useful than the other — narrow focus accordingly

---

## 7. Tech Stack

- **Language:** Python
- **Database:** SQLite (v1 scale doesn't need hosted Postgres)
- **Scheduling:** cron or a simple scheduled script (no task queue needed yet)
- **Interface:** CLI/scripts for v1 — no UI required until the hypothesis is
  validated enough to justify building one

---

## 8. Success Criteria for v1

This is framed as a discovery tool, not a pass/fail test of a single
hypothesis. Either outcome below counts as a successful v1:

- **If a news/price correlation shows up:** the price/expiry charting and
  manual review surfaced a set of flagged events where the pattern (large
  unusual position → subsequent price move / news) looks real enough to be
  worth investigating further, above what a naive baseline would suggest.
- **If no clear correlation shows up:** the tool still produced a curated
  shortlist of unusual options activity and associated wallets — data worth
  digging into on its own merits (a wallet's broader activity pattern,
  repeat unusual behavior, etc.), independent of whether it ties back to any
  specific news event.

In both cases, v1 succeeds if there's clean, queryable data and a
manageable, sustainable manual-review workflow — not contingent on the
original news-correlation hypothesis specifically panning out.

## 9. v2 Pivot — Watchlist & Paper-Trading Agent

The news/Trump-catalyst thread (CATALYSTS tab, `/api/news`, `/api/trump-posts`)
is dormant, not removed — deprioritized because it wasn't producing anything
worth the ongoing cost of the (still-uncached) live news lookups. All of that
code and data is untouched and can be revisited.

Focus shifted instead to a forward-looking test of the wallet-analysis work
this project had already built: `copy_candidates` (win-rate persistence
validated across a formation/validation split, positive total return in both
halves, survived a real RFQ-pricing-artifact bug fix — see conversation
history) feeds a curated `watchlist` table, which `scripts/run_watchlist_agent.py`
mirrors going forward with **simulated** paper trades (no real orders, no
credentials) — same settlement-pricing methodology the backtest was
validated on (`analysis/pnl.py`, shared with `api/main.py` so the two can't
drift apart the way an earlier P&L split did).

**Running it**: registered as a Windows Scheduled Task (`UndertowWatchlistAgent`,
every 15 minutes, `schtasks /query /tn UndertowWatchlistAgent /v` to check
status). Durable across reboots and independent of any Claude session — logs
to `data/watchlist_agent.log`. To change cadence or re-register after moving
the repo, re-run the `schtasks /create` command in
`scripts/run_watchlist_agent.bat`'s own comment header.

**Maintaining the watchlist**: `scripts/sync_watchlist.py` (re-run after
`scripts/build_copy_candidates.py`) auto-adds new `passes_screen` wallets via
either a large historical sample (100+ full-history positions, regardless of
recent activity) or a smaller sample with recent activity and a higher
Wilson bar (30+ positions, traded in the last 30 days, 65%+ Wilson) — added
after noticing several genuinely good, currently-active candidates were
being excluded purely by the 100-position cutoff. Quality (passes_screen)
is required either way; activity alone never qualifies a wallet. Removal is
manual (`DELETE /api/watchlist/{address}` or direct SQL) — deliberately not
automated yet, pending real paper-trading
outcome data to base a removal rule on.

**Dashboard**: PAPER TRADING tab — track record so far, per-source-wallet
breakdown, full trade ledger.

**Explicitly not built**: real order execution (Phase 4 in the approved
plan — `.env`-based Derive testnet credentials, a liquidity smoke-test,
`mode='testnet_order'` alongside the existing `paper_trades.mode='simulated'`).
Deferred until the simulated ledger has run long enough to be worth the
added credential/liquidity risk. See `C:\Users\iavit\.claude\plans\twinkling-percolating-riddle.md`
for the full phased plan and reasoning.

**Phase 4 sizing decision (settled in advance, not yet implemented)**:
real orders are capped at $10 of premium per trade -- Derive's minimum
order size is fixed per-instrument (0.01 BTC / 0.1 ETH contracts,
confirmed live via `/public/get_ticker`, not the same as a dollar
minimum), and checking it against 584 recent watchlist-wallet instruments
showed a $10 cap covers ~70% of trades at their exchange-enforced floor
(median floor $5.50, but the tail runs up to $436 on pricier
instruments). When Phase 4 is built: place the instrument's minimum
contract size if its cost is <= $10, otherwise skip mirroring that trade
(`skipped_min_size` or similar `fill_status`) rather than sizing up past
the cap -- trades coverage for a consistent, small, known-in-advance
dollar exposure per trade.
