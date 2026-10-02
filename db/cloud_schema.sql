-- Postgres schema for the hosted "live" database (Supabase free tier) --
-- NOT the full project schema. Only the tables the live path actually
-- touches (scripts/live_poll.py, run_watchlist_agent.py, run_live_agent.py,
-- resolve_paper_trades.py): current watchlist, each wallet's already-
-- computed ratings, and the paper/testnet trading ledger. Everything else
-- (price_snapshots, daily_signals, wallet_activity, copy_trade_backtest,
-- wallet_qualification_history, and the anomaly-detection columns on
-- options_events) stays local-only against the full 2.3GB SQLite archive,
-- per the hosting plan -- the heavy wallet-requalification pipeline that
-- needs that full history doesn't run in the cloud at all.
--
-- Translated from db/schema.sql, not reimplemented -- same table shapes,
-- same CHECK constraints, same indexes, with the handful of real
-- SQLite/Postgres differences applied deliberately:
--   - INTEGER PRIMARY KEY AUTOINCREMENT -> INTEGER GENERATED ALWAYS AS
--     IDENTITY PRIMARY KEY (standard-SQL form, Postgres's modern idiom).
--   - REAL -> DOUBLE PRECISION (matches SQLite's REAL, which is 8-byte;
--     Postgres's REAL is 4-byte/lower precision, not equivalent).
--   - Every unix-millisecond timestamp column -> BIGINT, not INTEGER.
--     Postgres's INTEGER is a fixed 4-byte type (max ~2.1 billion); real
--     unix-ms "now" is already ~1.78 trillion. SQLite's INTEGER is
--     dynamically up to 8 bytes so this was never an issue there -- it
--     would be a silent overflow here if left as INTEGER. `expiry`
--     (unix seconds, not ms) is BIGINT too for the same reason, one
--     degree further out (~2038 for a 32-bit signed second counter) but
--     this is live, ongoing infrastructure, not a static one-off dataset.
--   - 0/1 flag columns (is_active, is_likely_bot, pnl_is_estimated, etc.)
--     stay INTEGER, not a native BOOLEAN -- every script that reads/
--     writes these already does so as plain 0/1 Python ints, matching
--     SQLite's lack of a real boolean type; changing the column type
--     would mean changing call sites for no behavioral benefit.

CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    address TEXT NOT NULL UNIQUE,
    chain TEXT NOT NULL,
    first_seen BIGINT NOT NULL,
    label TEXT,
    alias TEXT
);

CREATE TABLE IF NOT EXISTS options_events (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('deribit', 'derive')),
    asset TEXT NOT NULL CHECK (asset IN ('BTC', 'ETH', 'SOL')),
    instrument TEXT NOT NULL,
    strike DOUBLE PRECISION NOT NULL,
    expiry BIGINT NOT NULL,
    option_type TEXT NOT NULL CHECK (option_type IN ('call', 'put')),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    size DOUBLE PRECISION NOT NULL,
    price DOUBLE PRECISION,
    notional_usd DOUBLE PRECISION,
    index_price_usd DOUBLE PRECISION,
    wallet_address TEXT,
    tx_hash TEXT,
    source_trade_id TEXT NOT NULL,
    timestamp BIGINT NOT NULL,
    mark_price DOUBLE PRECISION,
    rfq_id TEXT,
    tx_status TEXT,
    trade_fee DOUBLE PRECISION,
    liquidity_role TEXT,
    realized_pnl DOUBLE PRECISION,
    raw_json TEXT,
    UNIQUE (source, source_trade_id, wallet_address)
);
CREATE INDEX IF NOT EXISTS idx_options_events_asset_ts ON options_events (asset, timestamp);
CREATE INDEX IF NOT EXISTS idx_options_events_wallet ON options_events (wallet_address);

CREATE TABLE IF NOT EXISTS watchlist (
    wallet_address TEXT PRIMARY KEY REFERENCES wallets(address),
    added_at BIGINT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('auto', 'manual')),
    added_reason TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    removed_at BIGINT,
    removed_reason TEXT,
    last_checked_ts BIGINT,
    live_last_checked_ts BIGINT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS ingest_state (
    source TEXT PRIMARY KEY,
    last_ts BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_ts BIGINT NOT NULL,
    ingested_count INTEGER NOT NULL,
    opened_count INTEGER NOT NULL,
    advanced_count INTEGER NOT NULL,
    resolved_count INTEGER NOT NULL,
    skipped_naked_count INTEGER NOT NULL DEFAULT 0,
    skipped_weak_edge_count INTEGER NOT NULL DEFAULT 0,
    stopped_out_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_ts ON agent_runs (run_ts);

CREATE TABLE IF NOT EXISTS wallet_qualification (
    wallet_address TEXT PRIMARY KEY,
    total_n INTEGER NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('insufficient_data', 'does_not_qualify', 'qualified_but_faded', 'provisional', 'validated', 'high_confidence')),
    qualified_at_n INTEGER,
    qualified_at_ts BIGINT,
    pre_qualification_n INTEGER,
    pre_qualification_win_rate DOUBLE PRECISION,
    pre_qualification_wilson_low DOUBLE PRECISION,
    pre_qualification_total_return_pct DOUBLE PRECISION,
    forward_n INTEGER,
    forward_win_rate DOUBLE PRECISION,
    forward_wilson_low DOUBLE PRECISION,
    forward_total_return_pct DOUBLE PRECISION,
    forward_median_return_pct DOUBLE PRECISION,
    risk_discipline_score DOUBLE PRECISION,
    rfq_pct DOUBLE PRECISION,
    sizing_cv DOUBLE PRECISION,
    is_likely_bot INTEGER NOT NULL DEFAULT 0,
    computed_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_qualification_tier ON wallet_qualification (tier);

CREATE TABLE IF NOT EXISTS wallet_edge_profile (
    wallet_address TEXT NOT NULL,
    asset TEXT NOT NULL,
    option_type TEXT NOT NULL,
    entry_side TEXT NOT NULL,
    n INTEGER NOT NULL,
    win_rate DOUBLE PRECISION NOT NULL,
    wilson_low DOUBLE PRECISION NOT NULL,
    median_return_pct DOUBLE PRECISION NOT NULL,
    avg_return_pct DOUBLE PRECISION NOT NULL,
    return_lcb DOUBLE PRECISION,
    top1_gain_share DOUBLE PRECISION,
    computed_at BIGINT NOT NULL,
    PRIMARY KEY (wallet_address, asset, option_type, entry_side)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_wallet_address TEXT NOT NULL,
    source_event_id INTEGER NOT NULL REFERENCES options_events(id) UNIQUE,
    instrument TEXT NOT NULL,
    asset TEXT NOT NULL,
    option_type TEXT NOT NULL,
    strike DOUBLE PRECISION NOT NULL,
    expiry BIGINT NOT NULL,
    side TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('simulated', 'testnet_order')),
    intended_price DOUBLE PRECISION,
    entry_price DOUBLE PRECISION,
    entry_ts BIGINT,
    fill_status TEXT NOT NULL CHECK (fill_status IN ('pending_entry', 'open', 'resolved', 'skipped_stale', 'skipped_naked', 'skipped_weak_edge', 'stopped_out')),
    testnet_order_id TEXT,
    resolved_ts BIGINT,
    exit_price DOUBLE PRECISION,
    return_pct DOUBLE PRECISION,
    pnl_is_estimated INTEGER NOT NULL DEFAULT 0,
    loss_capped INTEGER NOT NULL DEFAULT 0,
    wallet_exit_price DOUBLE PRECISION,
    wallet_return_pct DOUBLE PRECISION,
    wallet_pnl_is_estimated INTEGER,
    edge_bucket_n INTEGER,
    edge_bucket_win_rate DOUBLE PRECISION,
    edge_bucket_wilson_low DOUBLE PRECISION,
    edge_bucket_median_return_pct DOUBLE PRECISION,
    notes TEXT,
    created_at BIGINT NOT NULL,
    margin_required_usd DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_wallet ON paper_trades (source_wallet_address);
CREATE INDEX IF NOT EXISTS idx_paper_trades_open ON paper_trades (fill_status) WHERE fill_status = 'open';

CREATE TABLE IF NOT EXISTS paper_trade_marks (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    paper_trade_id INTEGER NOT NULL REFERENCES paper_trades(id),
    ts BIGINT NOT NULL,
    mark_price DOUBLE PRECISION,
    index_price DOUBLE PRECISION,
    unrealized_return_pct DOUBLE PRECISION,
    best_bid_price DOUBLE PRECISION,
    best_ask_price DOUBLE PRECISION,
    delta DOUBLE PRECISION,
    gamma DOUBLE PRECISION,
    theta DOUBLE PRECISION,
    vega DOUBLE PRECISION,
    iv DOUBLE PRECISION,
    rho DOUBLE PRECISION,
    dte_days DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_paper_trade_marks_trade ON paper_trade_marks (paper_trade_id);
