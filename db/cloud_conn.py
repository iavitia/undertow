"""Postgres connection for the live path -- the scripts that need to run
unattended in GitHub Actions against the hosted Supabase DB (ingest,
candidate detection, paper-trade settlement, real testnet order
placement). NOT used by the heavy, local-only scripts
(build_wallet_qualification.py, build_trade_confidence.py,
backtest_copy_trades.py, etc.), which keep using sqlite3 against the
full local archive, unchanged -- see the hosting plan's reasoning for why
that split exists (the local DB is 2.3GB; the live path only ever needs
the current watchlist, each wallet's already-computed ratings, and a
rolling window of recent trade activity, not the full history behind
them).

CloudConnection wraps a psycopg connection behind the same surface the
live-path scripts already call against sqlite3.Connection --
conn.execute(sql, params).fetchone()/.fetchall(), conn.executemany(...),
conn.commit(), conn.close() -- with dict-style row access (row["col"])
matching conn.row_factory = sqlite3.Row, which every one of those scripts
already sets. The intent is that those scripts' own SQL and control flow
barely change -- only which connection object they're handed -- per the
hosting plan.

Two concrete SQLite/Postgres gaps this bridges, both confirmed by
inspection and a live test against the real local DB, not assumed:
  1. SQL throughout this project uses '?' placeholders (sqlite3's
     paramstyle); Postgres/psycopg wants '%s'. Translated here via a
     plain string replace -- safe because no query anywhere in this
     project embeds a literal '?' inside a string value; if that ever
     changes, this needs a real SQL-aware translator instead.
  2. `PRAGMA busy_timeout = ...` calls (scripts/run_watchlist_agent.py,
     scripts/backfill_derive.py) are meaningless on Postgres -- a real
     client-server database with proper MVCC, not SQLite's single-writer
     file lock (the actual cause of the repeated multi-hour hangs this
     project hit locally). Silently no-op'd here rather than requiring
     every call site to special-case it.

Not bridged, and not needed: SQLite's MIN(a,b) two-argument scalar form
vs. Postgres's aggregate-only MIN() -- the one place this project relied
on that (scripts/backfill_derive.py's upsert_trade) was rewritten to a
plain CASE expression, which both engines accept identically, rather than
patched here.
"""
import os
import sqlite3

import psycopg
from psycopg.rows import dict_row

# What every live-path script's `except sqlite3.IntegrityError:` (used to
# gracefully skip an already-inserted row on a UNIQUE constraint, e.g.
# paper_trades.source_event_id) needs to catch instead, since that
# exception type won't match what psycopg raises for the identical
# condition against the cloud DB. psycopg.IntegrityError is the top-level
# DB-API-2.0 class (constraint-violation subclasses like
# psycopg.errors.UniqueViolation all inherit from it), same role
# sqlite3.IntegrityError plays for SQLite.
INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)


class _NoopCursor:
    """Returned for PRAGMA statements, which Postgres doesn't have an
    equivalent concept for. Harmless if a caller reads from it (none
    currently do -- every PRAGMA call site in this project ignores the
    return value)."""

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class CloudConnection:
    def __init__(self, dsn):
        # prepare_threshold=None: disables psycopg3's default auto-prepare
        # (server-side PREPARE after a statement's 5th use). Required for
        # Supabase's transaction-mode pooler (Supavisor/PgBouncer) -- a
        # prepared statement doesn't survive the pooler handing this
        # connection's next query to a different backend, which every
        # live-path script here would hit eventually (e.g. live_poll.py's
        # ingest() re-running the same upsert_trade() INSERT well past 5
        # times in one run).
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False, prepare_threshold=None)

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith("PRAGMA"):
            return _NoopCursor()
        cur = self._conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(sql.replace("?", "%s"), seq_of_params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_cloud_conn():
    """CLOUD_DATABASE_URL: the Supabase (or any) Postgres connection
    string -- a GitHub Actions secret in the hosted workflows, a local
    .env value when testing the live path against the cloud DB from this
    machine."""
    dsn = os.environ["CLOUD_DATABASE_URL"]
    return CloudConnection(dsn)


def get_live_conn():
    """What every live-path script's run() calls instead of
    sqlite3.connect(DB_PATH) directly -- the one place "which database"
    gets decided, so none of those scripts need a flag or any other
    change to run correctly in either place. CLOUD_DATABASE_URL set (the
    GitHub Actions secret, injected as an env var in the hosted
    workflows) -> the cloud Postgres DB; unset (every local run, since
    nothing sets this in .env by default) -> local SQLite against
    config.DB_PATH, exactly as before this file existed."""
    if os.environ.get("CLOUD_DATABASE_URL"):
        return get_cloud_conn()

    import sqlite3

    from config import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
