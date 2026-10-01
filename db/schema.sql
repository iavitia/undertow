CREATE TABLE IF NOT EXISTS options_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK (source IN ('deribit', 'derive')),
    -- SOL added (see conversation): only this table needed it -- the
    -- wallet-qualification/copy-trading pipeline reads options_events
    -- directly, while price_snapshots/daily_signals below belong to the
    -- separate anomaly-detection/Signals feature, which hasn't been
    -- extended to SOL and doesn't need to be for this.
    asset TEXT NOT NULL CHECK (asset IN ('BTC', 'ETH', 'SOL')),
    instrument TEXT NOT NULL,
    strike REAL NOT NULL,
    expiry INTEGER NOT NULL,               -- unix seconds, options settlement time
    option_type TEXT NOT NULL CHECK (option_type IN ('call', 'put')),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    size REAL NOT NULL,                    -- contracts, in units of underlying
    price REAL,                            -- trade price (premium per contract)
    notional_usd REAL,                     -- size * index_price_usd at trade time
    index_price_usd REAL,
    wallet_address TEXT,                   -- derive only; null for deribit
    tx_hash TEXT,                          -- derive only
    source_trade_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,            -- unix ms
    mark_price REAL,                       -- Derive's mark price at execution
    rfq_id TEXT,                           -- non-null if this leg was RFQ block-negotiated
    tx_status TEXT,                        -- settled/reverted/timed_out
    trade_fee REAL,
    liquidity_role TEXT,                   -- maker/taker
    realized_pnl REAL,                     -- non-null/non-zero mainly on closing trades
    raw_json TEXT,                         -- full raw Derive trade record, for fields not worth a dedicated column (no greeks here -- Derive only exposes greeks on the live ticker snapshot, not per historical trade)
    is_flagged INTEGER NOT NULL DEFAULT 0,
    flag_reason TEXT,                      -- comma-joined: 'large_trade', 'wallet_size_jump', or both
    anomaly_score REAL,                    -- large_trade detector: size z-score vs market
    wallet_jump_ratio REAL,                -- wallet_size_jump detector: trade size / wallet's own prior average
    UNIQUE (source, source_trade_id, wallet_address)
);

CREATE INDEX IF NOT EXISTS idx_options_events_asset_ts ON options_events (asset, timestamp);
CREATE INDEX IF NOT EXISTS idx_options_events_wallet ON options_events (wallet_address);
-- partial index: is_flagged is true for <1% of rows, so a full-table index
-- would be wasted -- this keeps the dashboard's flagged-event queries from
-- doing a 728k-row scan every time (was ~3s per request, unindexed)
CREATE INDEX IF NOT EXISTS idx_options_events_flagged ON options_events (wallet_address) WHERE is_flagged = 1;

CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL UNIQUE,
    chain TEXT NOT NULL,
    first_seen INTEGER NOT NULL,           -- unix ms, earliest activity observed
    label TEXT,                            -- only ever set from a publicly known label, never inferred
    alias TEXT                             -- deterministic codename for UI readability only, not an identity claim
);

-- Broadened from the original (activity_type IN options_trade/transfer/swap)
-- once Phase 6 was actually built: Derive Chain's Blockscout API surfaces
-- ERC-4337 smart-account mechanics (sendBatch/handleOps/verifyAndMatch --
-- margin settlement, staking) alongside genuine bridge deposit/withdrawal
-- intents, and those are different enough in meaning that lumping them
-- under 'transfer' would be misleading. `method` keeps the raw contract
-- method name so the classification stays checkable against ground truth.
CREATE TABLE IF NOT EXISTS wallet_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER NOT NULL REFERENCES wallets (id),
    tx_hash TEXT NOT NULL,
    log_index INTEGER,
    timestamp INTEGER NOT NULL,
    activity_type TEXT NOT NULL CHECK (activity_type IN ('deposit', 'withdrawal', 'transfer', 'swap', 'staking', 'options_trade')),
    method TEXT,                           -- raw contract method, e.g. executeDepositIntent
    direction TEXT CHECK (direction IN ('in', 'out')),
    asset TEXT NOT NULL,                   -- token symbol
    amount REAL NOT NULL,
    usd_value REAL,
    counterparty_address TEXT,
    counterparty_label TEXT,               -- Blockscout's contract name for the counterparty, if known
    related_options_event_id INTEGER REFERENCES options_events (id),
    UNIQUE (tx_hash, log_index)
);

-- Curated wallets worth a standing deep-dive -- pinned to the top of the
-- Wallets tab regardless of flagged-notional ranking. Grown manually as
-- interesting wallets turn up, same pattern as the CATALYSTS list in
-- api/main.py.
CREATE TABLE IF NOT EXISTS featured_wallets (
    address TEXT PRIMARY KEY,
    note TEXT,
    added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL CHECK (asset IN ('BTC', 'ETH')),
    timestamp INTEGER NOT NULL,            -- unix ms
    price_usd REAL NOT NULL,
    source TEXT NOT NULL,
    UNIQUE (asset, timestamp, source)
);

-- Day-level anomaly signals (volume spikes, put/call skew shifts). Not in
-- the original data model -- added because these are a different grain
-- than options_events (one row per day+asset, not per trade) and don't fit
-- cleanly onto individual trade rows.
CREATE TABLE IF NOT EXISTS daily_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL CHECK (asset IN ('BTC', 'ETH')),
    day TEXT NOT NULL,                     -- ISO date, UTC
    total_notional_usd REAL NOT NULL,
    trade_count INTEGER NOT NULL,
    call_put_skew REAL NOT NULL,           -- call_notional / (call_notional + put_notional)
    volume_zscore REAL,
    skew_zscore REAL,
    is_volume_spike INTEGER NOT NULL DEFAULT 0,
    is_skew_shift INTEGER NOT NULL DEFAULT 0,
    UNIQUE (asset, day)
);

-- Cache for /api/trump-posts: the Factbase archive has no working
-- server-side date-range filter, so each lookup binary-searches ~1900
-- pages of date-descending pagination (~11 round trips) then walks
-- forward -- observed ~30s end to end. These are fixed historical windows
-- that won't change, so cache by (start_date, end_date) rather than
-- re-running the search every time the same catalyst is reopened.
CREATE TABLE IF NOT EXISTS trump_posts_cache (
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    items_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (start_date, end_date)
);

-- Precomputed per-wallet P&L + trading-pattern rollup, one row per wallet
-- that has ever placed a trade. The underlying aggregation (group 728k+
-- rows by wallet+instrument, then by wallet) takes ~6s -- fine for a
-- one-time rebuild, not for an on-demand API call -- so this is rebuilt by
-- scripts/build_leaderboard.py after each backfill/detection run and read
-- from directly at request time.
CREATE TABLE IF NOT EXISTS wallet_leaderboard (
    wallet_address TEXT PRIMARY KEY,
    positions INTEGER NOT NULL,            -- distinct instruments ever traded
    resolved INTEGER NOT NULL,             -- positions with nonzero net realized P&L
    wins INTEGER NOT NULL,
    losses INTEGER NOT NULL,
    win_rate REAL,                         -- wins / resolved, null if resolved = 0
    total_pnl REAL NOT NULL,               -- gross realized P&L
    total_fees REAL NOT NULL,
    net_of_fees REAL NOT NULL,
    total_legs INTEGER NOT NULL,
    legs_per_position REAL NOT NULL,       -- high = lots of partial fills per position
    active_days INTEGER NOT NULL,
    legs_per_day REAL NOT NULL,            -- high = high-frequency
    is_likely_bot INTEGER NOT NULL DEFAULT 0,  -- heuristic: legs_per_position > 8 OR legs_per_day > 15
    call_pct REAL,                         -- % of legs that were calls (vs puts)
    buy_pct REAL,                          -- % of legs that were buys (vs sells)
    btc_pct REAL,                          -- % of legs in BTC (vs ETH)
    avg_notional_usd REAL,
    first_ts INTEGER NOT NULL,
    last_ts INTEGER NOT NULL,
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_leaderboard_pnl ON wallet_leaderboard (net_of_fees);
CREATE INDEX IF NOT EXISTS idx_wallet_leaderboard_winrate ON wallet_leaderboard (win_rate);

-- Per-wallet rollup of scripts/backtest_copy_trades.py: "if a copier had
-- mirrored every one of this wallet's opens, entering at the next
-- available market print (any other wallet, same instrument) instead of
-- their exact fill, would it have been profitable?" Answers a different
-- question than wallet_leaderboard -- a wallet's own win rate doesn't
-- automatically transfer to someone copying it late. See conversation.
CREATE TABLE IF NOT EXISTS copy_trade_backtest (
    wallet_address TEXT PRIMARY KEY,
    copy_positions INTEGER NOT NULL,        -- positions where a copy entry+exit price was findable
    copy_wins INTEGER NOT NULL,
    copy_losses INTEGER NOT NULL,
    copy_win_rate REAL,                     -- copy_wins / copy_positions
    copy_win_rate_wilson_low REAL,          -- 95% Wilson lower bound -- the honest ranking metric, penalizes small samples
    copy_avg_return_pct REAL,               -- mean return on premium across copy positions
    copy_median_return_pct REAL,
    copy_total_return_pct REAL,             -- sum of per-position returns (not compounded)
    excluded_stale INTEGER NOT NULL,        -- positions skipped: no other-wallet print within the staleness window
    excluded_open INTEGER NOT NULL,         -- positions skipped: not yet resolved (still open, not expired)
    copy_worst_return_pct REAL,             -- min single-position return -- the tail-risk side of the story win_rate hides
    copy_best_return_pct REAL,              -- max single-position return -- context for whether wins offset that tail
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_copy_trade_backtest_wilson ON copy_trade_backtest (copy_win_rate_wilson_low);

-- scripts/backtest_stop_loss.py: for specific wallets whose blowup was
-- investigated (see conversation -- cedar-ridge-e9, iron-grove-22, and
-- others all had a good win rate wiped out by naked shorts held to expiry
-- with no stop-loss), replays their copy_trade_backtest positions three
-- ways: 'actual' (unmodified -- what really would have happened copying
-- them, naked shorts included), 'protected' (naked shorts capped at
-- stop_multiple x premium collected, using nearest available spot price +
-- intrinsic value as a historical proxy for buy-back cost -- there's no
-- historical mark/greeks data to do better than that), and 'skipped'
-- (naked shorts excluded entirely -- buy-side positions only, matching
-- what scripts/run_watchlist_agent.py's live gate already does today).
-- Answers "would a stop-loss have actually saved these wallets, and is it
-- better than just not taking the naked side at all," not a live
-- mechanism -- see this table's docstring in scripts/backtest_stop_loss.py
-- for why intrinsic-value-only likely understates how early a real stop
-- would trigger.
CREATE TABLE IF NOT EXISTS stop_loss_backtest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('actual', 'protected', 'skipped')),
    n INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    win_rate REAL,
    total_return_pct REAL,
    worst_return_pct REAL,
    best_return_pct REAL,
    n_stopped_out INTEGER NOT NULL DEFAULT 0,  -- always 0 for mode='actual'
    stop_multiple REAL NOT NULL,               -- 3.0 for this run -- kept so a different threshold's run doesn't silently overwrite this one
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stop_loss_backtest_wallet ON stop_loss_backtest (wallet_address);

-- Per-position detail for every naked short that actually breached the
-- cap in a stop_loss_backtest run -- lets a specific position (e.g.
-- cedar-ridge-e9's BTC $65,000 call) be checked concretely, not just read
-- off an aggregate delta.
CREATE TABLE IF NOT EXISTS stop_loss_backtest_stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    instrument TEXT NOT NULL,
    entry_price REAL NOT NULL,
    original_exit_value REAL NOT NULL,
    original_return_pct REAL NOT NULL,
    original_resolved_ts INTEGER NOT NULL,
    stopped_exit_value REAL NOT NULL,
    stopped_return_pct REAL NOT NULL,
    stop_ts INTEGER NOT NULL,
    stop_multiple REAL NOT NULL,
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stop_loss_backtest_stops_wallet ON stop_loss_backtest_stops (wallet_address);

-- scripts/analyze_stop_sensitivity.py: sweeps multiple stop thresholds
-- (not just the single 3x scripts/backtest_stop_loss.py tests) across two
-- wallet populations -- 'blown_up_8' (the fixed historical reference set
-- that motivated the whole stop-loss investigation) and
-- 'current_watchlist' (whatever's actually is_active=1 right now, so this
-- naturally drifts as refresh_qualification.py adds/removes wallets --
-- that's intentional, not stale data). stop_multiple IS NULL is the
-- no-stop baseline (same numbers as stop_loss_backtest's 'actual' mode,
-- recomputed here for a single self-contained sweep table rather than
-- joined across two tables). See conversation for why P&L is summed
-- across independent positions in resolution order rather than a true
-- funded-portfolio equity curve -- max_drawdown_usd is explicitly that
-- same simplification, not a claim about real capital at risk.
CREATE TABLE IF NOT EXISTS stop_sensitivity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_set TEXT NOT NULL,
    stop_multiple REAL,
    n INTEGER NOT NULL,
    win_rate REAL,
    total_return_pct REAL,
    worst_return_pct REAL,
    profit_factor REAL,
    max_drawdown_usd REAL,
    n_stopped INTEGER NOT NULL DEFAULT 0,
    pnl_saved_pct REAL,       -- sum of (protected - original) where protected is better -- the stop was correct
    pnl_forfeited_pct REAL,   -- sum of (original - protected) where protected is worse -- a whipsaw
    n_whipsaw INTEGER NOT NULL DEFAULT 0,
    false_stop_rate REAL,     -- n_whipsaw / n_stopped
    -- $10/trade-normalized versions of pnl_saved_pct/pnl_forfeited_pct,
    -- persisted explicitly rather than converted on read each time.
    pnl_saved_usd REAL,
    pnl_forfeited_usd REAL,
    net_benefit_usd REAL,     -- pnl_saved_usd - pnl_forfeited_usd
    -- Stop-slippage/gap stats: for triggered stops, how far the actual
    -- exit value landed past the clean theoretical cap (stop_multiple *
    -- entry_price) -- 0 = landed exactly on the cap, higher = the next
    -- available print had already jumped past it (see conversation: the
    -- position whose worst-case return was identical across every tested
    -- multiple, caused by exactly this -- a low-liquidity gap breaching
    -- several thresholds in the same jump).
    avg_overshoot_pct REAL,
    median_overshoot_pct REAL,
    p90_overshoot_pct REAL,
    -- Time-to-stop, in hours from entry to the triggering print (see
    -- conversation: below ~1.1x, does the sweep keep improving because
    -- of genuine mid-life risk protection, or because an ultra-tight
    -- stop mostly just detects that the copy-entry print itself was
    -- already stale/adverse -- an entry-confirmation effect rather than
    -- a stop-loss effect? If time-to-stop collapses toward ~0 and
    -- pct_triggered_at_first_print rises as the multiple tightens,
    -- that's the entry-confirmation signature).
    median_time_to_stop_hours REAL,
    p10_time_to_stop_hours REAL,
    p90_time_to_stop_hours REAL,
    pct_triggered_at_first_print REAL,
    -- Same total_return_pct/net_benefit_usd, recomputed with every
    -- first-print-triggered stop treated as never having fired (falls
    -- back to the position's real, unprotected outcome) -- isolates how
    -- much of a tight multiple's advantage is genuine mid-life risk
    -- management vs. the entry-confirmation artifact quantified above.
    total_return_pct_excl_first_print REAL,
    net_benefit_usd_excl_first_print REAL,
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stop_sensitivity_set ON stop_sensitivity (wallet_set);

-- Per-wallet (not pooled) MAE distribution, split by whether the
-- position eventually won or lost -- the direct input for
-- scripts/analyze_stop_wallet_correlation.py's check of whether wallets
-- hurt by a tight stop share a measurable MAE-shape characteristic.
CREATE TABLE IF NOT EXISTS wallet_mae_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    wallet_set TEXT NOT NULL,
    outcome_group TEXT NOT NULL CHECK (outcome_group IN ('winner', 'loser')),
    n INTEGER NOT NULL,
    median_mae_multiple REAL,
    p90_mae_multiple REAL,
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_mae_summary_wallet ON wallet_mae_summary (wallet_address);

-- scripts/analyze_stop_wallet_correlation.py: does being hurt by a tight
-- stop (majority vote across the 1.1x-2.5x fine grid, see
-- stop_sensitivity) correlate with anything already computed about a
-- wallet -- checked before considering wallet-specific stop thresholds
-- (see conversation -- fitting a separate threshold per wallet risks
-- overfitting noise at the sample sizes most watchlist wallets have; this
-- is the cheap check to run first).
CREATE TABLE IF NOT EXISTS stop_wallet_correlation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    classification TEXT NOT NULL CHECK (classification IN ('consistently_hurt', 'consistently_helped', 'mixed')),
    risk_discipline_score REAL,
    rfq_pct REAL,
    sizing_cv REAL,
    total_n INTEGER,
    forward_n INTEGER,
    winner_median_mae REAL,
    winner_p90_mae REAL,
    loser_median_mae REAL,
    loser_p90_mae REAL,
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stop_wallet_correlation_class ON stop_wallet_correlation (classification);

-- Per-position Maximum Adverse/Favorable Excursion, independent of any
-- specific stop threshold -- the raw distribution stop_sensitivity's
-- aggregates are computed from, kept for direct inspection (e.g. "how far
-- do eventual winners typically move against us before recovering" needs
-- the distribution, not just an aggregate). Sell positions (naked
-- shorts) only -- a buy's loss is already capped, no excursion analysis
-- needed. mae_multiple/mfe_multiple are the worst/best point reached in
-- the position's life, as a multiple of entry_price (buy-back cost at
-- that point / premium collected) -- mae is how many multiples of
-- premium it would have cost to close at the worst moment, mfe is the
-- cheapest (mfe near 0 for a position that was ever fully worthless
-- along the way).
CREATE TABLE IF NOT EXISTS position_mae_mfe (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    wallet_set TEXT NOT NULL,
    instrument TEXT NOT NULL,
    entry_price REAL NOT NULL,
    mae_multiple REAL,
    mfe_multiple REAL,
    actual_return_pct REAL NOT NULL,
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_position_mae_mfe_wallet ON position_mae_mfe (wallet_address);

-- scripts/backtest_walkforward.py: does a wallet's copy_trade_backtest
-- performance in an earlier "formation" period predict its performance in
-- a later "validation" period? Only wallets with enough resolved copy
-- positions on BOTH sides of the split are included -- see conversation.
-- SUPERSEDED by wallet_qualification below -- both this table and
-- copy_candidates split each wallet's history at ONE point (a shared
-- global date, or a per-wallet midpoint) and just compared the two
-- halves, which has two flaws identified in conversation: (1) a wallet
-- whose entire history starts after the global cutoff has zero formation
-- data and can never be evaluated, no matter how good it is (drift-vault-68);
-- (2) comparing two arbitrary halves doesn't ask "would we have actually
-- selected this wallet at the time," so it can show spurious persistence
-- with no real point-in-time decision behind it (look-ahead bias). Kept
-- in the schema for historical/audit reference; nothing computes into
-- these anymore.
CREATE TABLE IF NOT EXISTS walkforward_backtest (
    wallet_address TEXT PRIMARY KEY,
    cutoff_ts INTEGER NOT NULL,
    formation_n INTEGER NOT NULL,
    formation_win_rate REAL,
    formation_wilson_low REAL,
    formation_median_return_pct REAL,
    formation_avg_return_pct REAL,
    validation_n INTEGER NOT NULL,
    validation_win_rate REAL,
    validation_wilson_low REAL,
    validation_median_return_pct REAL,
    validation_avg_return_pct REAL,
    computed_at INTEGER NOT NULL
);

-- SUPERSEDED by wallet_qualification below -- see that table's comment.
-- Kept for historical/audit reference; nothing computes into this anymore.
CREATE TABLE IF NOT EXISTS copy_candidates (
    wallet_address TEXT PRIMARY KEY,
    formation_win_rate REAL,
    formation_wilson_low REAL,
    formation_n INTEGER,
    formation_total_return_pct REAL,
    validation_win_rate REAL,
    validation_wilson_low REAL,
    validation_n INTEGER,
    validation_total_return_pct REAL,
    full_positions INTEGER,
    full_wilson_low REAL,
    full_total_return_pct REAL,
    full_worst_return_pct REAL,             -- single worst position, full history
    tail_ratio REAL,                        -- full_total_return_pct / |full_worst_return_pct| -- higher = gains outweigh the worst single loss by more
    is_likely_bot INTEGER NOT NULL DEFAULT 0,
    passes_screen INTEGER NOT NULL DEFAULT 0,
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_copy_candidates_tail_ratio ON copy_candidates (tail_ratio);

-- analysis/qualification.py: no-look-ahead, per-wallet qualification walk
-- (see conversation -- this replaces walkforward_backtest/copy_candidates'
-- fixed-split design). Walks a wallet's resolved copy-trade positions in
-- chronological order; the first point where its CUMULATIVE record clears
-- an absolute bar (Wilson lower bound >= 60%, calibrated against the real
-- population -- see conversation, that's the top ~7% of wallets with 15+
-- positions -- AND positive cumulative return, both using ONLY data up to
-- that point) is qualified_at_n. Everything after that point is the
-- forward_* record: a genuine, no-look-ahead answer to "if we'd started
-- copying this wallet the moment it qualified, what would have happened
-- since." A wallet that never clears the bar gets tier='does_not_qualify'
-- rather than silently having no row -- distinct from tier='insufficient_data'
-- (too few positions to evaluate at all).
CREATE TABLE IF NOT EXISTS wallet_qualification (
    wallet_address TEXT PRIMARY KEY,
    total_n INTEGER NOT NULL,               -- total resolved copy-eligible positions, full history
    tier TEXT NOT NULL CHECK (tier IN ('insufficient_data', 'does_not_qualify', 'qualified_but_faded', 'provisional', 'validated', 'high_confidence')),
    qualified_at_n INTEGER,                 -- position count at first qualification, NULL if never
    qualified_at_ts INTEGER,                -- resolved_ts of the qualifying position
    pre_qualification_n INTEGER,
    pre_qualification_win_rate REAL,
    pre_qualification_wilson_low REAL,
    pre_qualification_total_return_pct REAL,
    forward_n INTEGER,                      -- positions strictly after qualification -- the real forward test
    forward_win_rate REAL,
    forward_wilson_low REAL,
    forward_total_return_pct REAL,
    forward_median_return_pct REAL,
    -- 0-100, combines tail_ratio (capped), RFQ-cleanliness, and position-
    -- sizing consistency (coefficient of variation of notional_usd) --
    -- the one new synthesized score for v1 (see conversation: deliberately
    -- not building a full 5-axis system yet, since none of these
    -- combinations are validated against real outcomes -- start with what
    -- we can already ground in real numbers, expand later).
    risk_discipline_score REAL,
    rfq_pct REAL,                           -- fraction of legs that were RFQ-matched, context for risk_discipline_score
    sizing_cv REAL,                         -- coefficient of variation of notional_usd, context for risk_discipline_score
    is_likely_bot INTEGER NOT NULL DEFAULT 0,
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_qualification_tier ON wallet_qualification (tier);
CREATE INDEX IF NOT EXISTS idx_wallet_qualification_forward_wilson ON wallet_qualification (forward_wilson_low);

-- Append-only history of wallet_qualification (see conversation -- that
-- table is DELETE+reinserted whole on every scripts/build_wallet_qualification.py
-- run, so it only ever shows the current snapshot; there was no way to
-- answer "is this wallet's rating trending up or down" without this).
-- Same columns as wallet_qualification, one row per (wallet, run) instead
-- of one row per wallet -- written alongside the current-state table on
-- every real (non-skipped) requalification pass, for every wallet in the
-- universe that pass computes, not just watchlist wallets. Nothing reads
-- from this to make a live decision -- wallet_qualification (and
-- scripts/sync_watchlist.py, which reads it) stays the single source of
-- truth for current eligibility; this is purely for trend inspection.
CREATE TABLE IF NOT EXISTS wallet_qualification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    total_n INTEGER NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('insufficient_data', 'does_not_qualify', 'qualified_but_faded', 'provisional', 'validated', 'high_confidence')),
    qualified_at_n INTEGER,
    qualified_at_ts INTEGER,
    pre_qualification_n INTEGER,
    pre_qualification_win_rate REAL,
    pre_qualification_wilson_low REAL,
    pre_qualification_total_return_pct REAL,
    forward_n INTEGER,
    forward_win_rate REAL,
    forward_wilson_low REAL,
    forward_total_return_pct REAL,
    forward_median_return_pct REAL,
    risk_discipline_score REAL,
    rfq_pct REAL,
    sizing_cv REAL,
    is_likely_bot INTEGER NOT NULL DEFAULT 0,
    computed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_qualification_history_wallet_ts ON wallet_qualification_history (wallet_address, computed_at);

-- Curated, frozen watchlist for scripts/run_watchlist_agent.py -- deliberately
-- NOT a live read of copy_candidates (same reasoning as featured_wallets vs
-- the automated wallet ranking): if a wallet drops off the automated screen
-- while we have an open paper position against it, we still want to know we
-- were tracking it and why, not have it silently vanish from the query.
CREATE TABLE IF NOT EXISTS watchlist (
    wallet_address TEXT PRIMARY KEY REFERENCES wallets(address),
    added_at INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('auto', 'manual')),
    added_reason TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    removed_at INTEGER,
    removed_reason TEXT,
    last_checked_ts INTEGER,                -- high-water mark for new-trade polling, unix ms (scripts/run_watchlist_agent.py's paper-trading candidate stream)
    -- Independent high-water mark for scripts/run_live_agent.py (see
    -- conversation): must NOT share last_checked_ts above -- if the paper
    -- and live agents both consumed candidates off the same cursor,
    -- whichever one happened to run first would silently advance it past
    -- a candidate before the other ever saw it. Same source table
    -- (find_new_candidates() in scripts/run_watchlist_agent.py), two
    -- independent cursors, so both agents see every real candidate.
    live_last_checked_ts INTEGER,
    notes TEXT
);

-- High-water mark for scripts/live_poll.py's incremental ingest -- one row
-- per source feed (currently just 'derive_live').
CREATE TABLE IF NOT EXISTS ingest_state (
    source TEXT PRIMARY KEY,
    last_ts INTEGER NOT NULL
);

-- One row per scripts/run_watchlist_agent.py tick -- structured history of
-- the scheduled task's own activity (vs. parsing data/watchlist_agent.log,
-- the wrong tool for something the dashboard needs to query). Lets the
-- LIVE FEED tab show the pipeline is actually running on its 15-min
-- cadence, not just that it *should* be.
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts INTEGER NOT NULL,
    ingested_count INTEGER NOT NULL,
    opened_count INTEGER NOT NULL,
    advanced_count INTEGER NOT NULL,
    resolved_count INTEGER NOT NULL,
    -- Bounded-risk gating (see conversation, paper_trades.fill_status'
    -- comment): candidates that were skipped_naked or skipped_weak_edge
    -- this tick, tracked here so tick-level visibility (Live Feed) shows
    -- how much of what's detected isn't being acted on and why.
    skipped_naked_count INTEGER NOT NULL DEFAULT 0,
    skipped_weak_edge_count INTEGER NOT NULL DEFAULT 0,
    -- RETIRED (see conversation) -- always 0 going forward now that
    -- naked shorts are no longer closed early via a live mark-price poll
    -- (config.STOP_MULTIPLE), only floored at settlement
    -- (config.NAKED_SHORT_LOSS_FLOOR_PCT, see paper_trades.loss_capped).
    -- Column kept so historical rows from before the change stay readable.
    stopped_out_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_ts ON agent_runs (run_ts);

-- The paper-trading ledger scripts/run_watchlist_agent.py writes to and
-- scripts/resolve_paper_trades.py settles. mode='simulated' prices the
-- mirror using the same validated "next print by another wallet" /
-- expiry-settlement methodology as scripts/backtest_copy_trades.py, no real
-- order sent. mode='testnet_order' (Phase 4, not built yet) will add a real
-- Derive-testnet order alongside this same row shape.
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_wallet_address TEXT NOT NULL,
    source_event_id INTEGER NOT NULL REFERENCES options_events(id) UNIQUE,
    instrument TEXT NOT NULL,
    asset TEXT NOT NULL,
    option_type TEXT NOT NULL,
    strike REAL NOT NULL,
    expiry INTEGER NOT NULL,                -- unix seconds, matches options_events.expiry
    side TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('simulated', 'testnet_order')),
    intended_price REAL,                    -- the source wallet's own fill price, for reference
    entry_price REAL,                       -- our copy entry price (simulated: next-other-wallet-print; testnet_order: actual fill)
    entry_ts INTEGER,
    -- pending_entry: detected, passed both gates below, but no valid
    -- copy-price found yet (real copier's price may not have printed in
    -- the market yet either, or the only candidates so far share an
    -- rfq_id with the source wallet's own trade and are excluded -- see
    -- analysis/pnl.py's module docstring) -- retried on later runs until
    -- COPY_STALENESS_WINDOW_MS elapses, then becomes skipped_stale.
    -- open: has a real entry_price, tracking to expiry. resolved: settled.
    --
    -- skipped_weak_edge (see conversation): wallet_edge_profile has n>=5
    -- history for this wallet's exact (asset, option_type, side) combo
    -- and its wilson_low is below 50% -- worse than a coin flip in a
    -- combination this wallet has actually done enough of to judge. A
    -- combo with no bucket yet (untested, not proven-bad) still passes
    -- through -- absence of evidence isn't evidence of badness, and
    -- gating on it would mean a wallet's edge in something new to them
    -- could never be discovered. Assigned immediately at detection, never
    -- passes through pending_entry.
    --
    -- skipped_naked is a RETIRED status, kept only so historical rows from
    -- before the live stop-loss engine still satisfy this constraint --
    -- nothing produces it anymore. It used to mean "side='sell', no risk
    -- management exists yet, recorded for research but not acted on" (see
    -- the blowup investigation into cedar-ridge-e9, iron-grove-22, and
    -- others, which found every one of their worst losses was a naked
    -- short held to expiry with no stop-loss). Naked shorts are mirrored
    -- again now, protected by config.STOP_MULTIPLE and
    -- scripts/run_watchlist_agent.py's check_live_stops() -- see
    -- stopped_out below.
    --
    -- stopped_out is a RETIRED status (see conversation), kept only so the
    -- 48 historical rows from the live poll-triggered stop-loss engine's
    -- brief run still satisfy this constraint -- nothing produces it
    -- anymore. It used to mean "closed early because its live mark price
    -- (polled via clients/derive_client.get_ticker each tick) reached
    -- config.STOP_MULTIPLE x the premium collected." Retired after two
    -- days of live data showed the 15-minute poll cadence couldn't catch
    -- short-dated, high-gamma naked shorts before their mark gapped well
    -- past the intended cap (48 stopped-out trades averaged -63% realized
    -- loss, not the intended -10%; every one of the subset with a
    -- checkable wallet-own outcome would have been a win if left alone).
    -- Replaced by config.NAKED_SHORT_LOSS_FLOOR_PCT -- see resolved below
    -- and loss_capped.
    fill_status TEXT NOT NULL CHECK (fill_status IN ('pending_entry', 'open', 'resolved', 'skipped_stale', 'skipped_naked', 'skipped_weak_edge', 'stopped_out')),
    testnet_order_id TEXT,
    resolved_ts INTEGER,
    exit_price REAL,
    return_pct REAL,                        -- return on premium, same convention as copy_trade_backtest -- naked shorts (side='sell') are floored at config.NAKED_SHORT_LOSS_FLOOR_PCT (-100%), see loss_capped
    pnl_is_estimated INTEGER NOT NULL DEFAULT 0,
    -- 1 if this position's settlement math produced a loss worse than
    -- config.NAKED_SHORT_LOSS_FLOOR_PCT and scripts/resolve_paper_trades.py
    -- clamped it to the floor instead -- see conversation: replaces the
    -- retired live stop-loss engine (stopped_out above) with a hard cap
    -- applied at expiry instead of a live poll trying to catch it early.
    -- Always 0 for buy-side rows (already naturally capped at -100%, exit
    -- price can't go below 0).
    loss_capped INTEGER NOT NULL DEFAULT 0,
    -- What the SOURCE wallet's own position (not our copy) actually
    -- closed at -- see scripts/run_watchlist_agent.py's
    -- check_wallet_outcomes() (see conversation: added specifically to
    -- compare our stop-loss-protected outcome against what the wallet
    -- itself actually experienced, holding the exact same position with
    -- no protection). wallet_exit_price/wallet_return_pct use
    -- intended_price (their own entry fill, already stored) as the entry
    -- side of the same buy/sell return-on-premium formula used
    -- everywhere else in this project. Two ways to resolve, mirroring
    -- compute_wallet_positions in api/main.py exactly: (1) the wallet
    -- closed it themselves via a real offsetting trade -- wallet_exit_price
    -- is their own actual fill, wallet_pnl_is_estimated=0; (2) they never
    -- closed it and the instrument has since expired -- wallet_exit_price
    -- is the same nearest-index-price + intrinsic-value estimate used for
    -- our own expiry settlements, wallet_pnl_is_estimated=1. Left NULL
    -- (not yet determined) while the wallet's own position is still open
    -- and unexpired -- rechecked every tick until it resolves, independent
    -- of whatever our own fill_status is.
    wallet_exit_price REAL,
    wallet_return_pct REAL,
    wallet_pnl_is_estimated INTEGER,
    -- Trade-level confidence (see wallet_edge_profile below): the wallet's
    -- historical record in this exact (asset, option_type, side) bucket
    -- as of when this trade was detected. For rows that made it to
    -- pending_entry/open/resolved, this is annotation only (see
    -- conversation: no evidence yet that it's predictive beyond the
    -- skipped_weak_edge gate above, which only screens out the proven-bad
    -- end). For skipped_weak_edge rows, this **is** the reason it was
    -- skipped, populated at detection time instead of left null.
    edge_bucket_n INTEGER,
    edge_bucket_win_rate REAL,
    edge_bucket_wilson_low REAL,
    edge_bucket_median_return_pct REAL,
    notes TEXT,
    created_at INTEGER NOT NULL,
    -- Real margin held against a testnet_order row, read back from
    -- clients/derive_execution_client.get_account_state() right after
    -- placing it (see conversation -- there is no margin-preview
    -- endpoint, only post-hoc account state). Always NULL for
    -- mode='simulated' rows, which have no real margin concept.
    margin_required_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_wallet ON paper_trades (source_wallet_address);
CREATE INDEX IF NOT EXISTS idx_paper_trades_open ON paper_trades (fill_status) WHERE fill_status = 'open';

-- scripts/run_watchlist_agent.py's record_live_marks() (renamed from
-- check_live_stops() -- see conversation, it no longer closes anything):
-- one row per open naked-short paper_trades position per agent tick -- the
-- live "current mark -> unrealized P&L" tracking from the user's own
-- risk-management writeup, now including Greeks, collected prospectively
-- since a ticker poll is already made per instrument every tick anyway;
-- delta/theta/gamma/vega/iv/rho come straight off that same response's
-- option_pricing block, no extra API calls. Collection only, deliberately
-- -- nothing in this project reads these columns to make a trade decision
-- yet; that's a real follow-up once there's enough accumulated history to
-- check against, not before. Naked shorts are no longer closed early off
-- of this data (replaced by the settlement-time loss floor -- see
-- paper_trades.loss_capped); this table is now pure observability, not an
-- input to any live decision. Only sells get snapshotted -- a buy's risk
-- is already capped at 100% of premium paid, no monitoring needed.
CREATE TABLE IF NOT EXISTS paper_trade_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_trade_id INTEGER NOT NULL REFERENCES paper_trades(id),
    ts INTEGER NOT NULL,
    mark_price REAL,
    index_price REAL,
    unrealized_return_pct REAL,   -- (entry_price - mark_price) / entry_price, same sell-side formula as everywhere else
    best_bid_price REAL,
    best_ask_price REAL,
    delta REAL,
    gamma REAL,
    theta REAL,
    vega REAL,
    iv REAL,
    rho REAL,
    dte_days REAL   -- days to expiry at snapshot time, computed locally (not from the API) -- context for interpreting theta
);
CREATE INDEX IF NOT EXISTS idx_paper_trade_marks_trade ON paper_trade_marks (paper_trade_id);

-- scripts/build_trade_confidence.py: per-wallet performance broken out by
-- (asset, option_type, entry_side) instead of one overall number -- the
-- idea from conversation ("Wallet #382 is excellent at selling ETH
-- volatility but mediocre directionally... same wallet buys a huge SOL
-- call: REJECT") is that a wallet's edge is often concentrated in a
-- specific kind of trade, not uniform across everything it does. Built
-- from the exact same validated simulate_copy_trades() positions as
-- copy_trade_backtest, just grouped one level finer -- not a separate,
-- possibly-drifting computation.
CREATE TABLE IF NOT EXISTS wallet_edge_profile (
    wallet_address TEXT NOT NULL,
    asset TEXT NOT NULL,
    -- 'call' / 'put' for the normal per-option-type buckets, or the
    -- sentinel 'ALL' for the pooled (call+put) buy-side bucket -- see
    -- conversation: win-rate/Wilson is the wrong lens for buy-side edge
    -- (bounded-loss, unbounded/right-skewed-win payoff), so buy-side
    -- candidates are gated on the pooled bucket's return_lcb/
    -- top1_gain_share instead (analysis/qualification.buy_edge_lower_bound),
    -- which needs more data than the win-rate stat to be trustworthy, hence
    -- pooling call+put rather than splitting further. Sell-side keeps
    -- exactly the existing per-option-type win_rate/wilson_low gating,
    -- unchanged.
    option_type TEXT NOT NULL,
    entry_side TEXT NOT NULL,
    n INTEGER NOT NULL,
    win_rate REAL NOT NULL,
    wilson_low REAL NOT NULL,
    median_return_pct REAL NOT NULL,
    avg_return_pct REAL NOT NULL,
    -- Only populated on the pooled option_type='ALL' buy-side row.
    return_lcb REAL,
    top1_gain_share REAL,
    computed_at INTEGER NOT NULL,
    PRIMARY KEY (wallet_address, asset, option_type, entry_side)
);

-- related_event_table disambiguates which table related_event_id points to,
-- since the plan's schema allows annotating either options_events or
-- wallet_activity rows and SQLite has no polymorphic foreign keys.
CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    related_event_id INTEGER NOT NULL,
    related_event_table TEXT NOT NULL CHECK (related_event_table IN ('options_events', 'wallet_activity')),
    note TEXT,
    related_announcement_date INTEGER,
    related_announcement_summary TEXT,
    outcome_direction TEXT CHECK (outcome_direction IN ('aligned', 'not aligned', 'unclear')),
    added_at INTEGER NOT NULL
);
