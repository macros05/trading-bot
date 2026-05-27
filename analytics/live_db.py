"""SQLite storage for live paper-trading data.

Single file, append-friendly schema kept independent from the JSON
trades_history.json so the live record survives any future migration of the
JSON file format. Each table can be queried with arbitrary SQL via
analytics.live_db.query_all() for ad-hoc analysis.

Schema
------
live_trades   one row per closed paper-trade with rich entry/exit context
near_misses   one row per near-miss snapshot (sampled, not every tick)
kelly_changes one row per adaptive-Kelly adjustment
shadow_trades one row per shadow-mode hypothetical trade
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path('data/live_trades.db')


_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_ts_ms     INTEGER NOT NULL,
    exit_ts_ms      INTEGER NOT NULL,
    side            TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    exit_price      REAL    NOT NULL,
    qty             REAL    NOT NULL,
    notional_usdt   REAL    NOT NULL,
    pnl_usdt        REAL    NOT NULL,
    pnl_pct         REAL    NOT NULL,
    result          TEXT    NOT NULL,
    exit_reason     TEXT    NOT NULL,
    duration_min    REAL    NOT NULL,
    session         TEXT    NOT NULL,
    -- Entry-time market context
    entry_rsi       REAL,
    entry_adx       REAL,
    entry_atr       REAL,
    entry_atr_pct   REAL,            -- percentile of last 48h ATR window
    entry_sma20     REAL,
    entry_sma50     REAL,
    mtf_15m_aligned INTEGER,         -- 1 yes, 0 no, NULL warmup
    htf_4h_trend    TEXT,            -- 'up' | 'down' | NULL
    htf_daily_trend TEXT,
    regime          TEXT,            -- 'trending' | 'ranging' | 'volatile'
    macro_event     TEXT,            -- '' | 'FOMC' | 'CPI' | 'NFP' | 'WEEKEND'
    kelly_used      REAL,            -- effective risk_pct used at entry
    -- Free-form JSON for evolving fields without schema change
    extra_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_live_trades_entry_ts ON live_trades(entry_ts_ms);
CREATE INDEX IF NOT EXISTS idx_live_trades_session ON live_trades(session);

CREATE TABLE IF NOT EXISTS near_misses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    reason          TEXT    NOT NULL,
    close           REAL    NOT NULL,
    rsi             REAL    NOT NULL,
    sma20           REAL    NOT NULL,
    rsi_distance    REAL,            -- how far from threshold (signed)
    sma_distance_pct REAL,
    side_intended   TEXT,            -- 'long' | 'short'
    would_have_won  INTEGER,         -- backfilled later
    hypothetical_pnl_usdt REAL       -- backfilled when path resolves
);

CREATE INDEX IF NOT EXISTS idx_near_misses_ts ON near_misses(ts_ms);

CREATE TABLE IF NOT EXISTS kelly_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    old_kelly_pct   REAL    NOT NULL,
    new_kelly_pct   REAL    NOT NULL,
    rolling_win_rate REAL   NOT NULL,
    n_recent_trades INTEGER NOT NULL,
    reason          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_ts_ms  INTEGER NOT NULL,
    side            TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    sl_price        REAL    NOT NULL,
    tp_price        REAL    NOT NULL,
    -- Resolved later by replaying the candle stream
    exit_ts_ms      INTEGER,
    exit_price      REAL,
    exit_reason     TEXT,
    pnl_usdt        REAL,
    resolved        INTEGER DEFAULT 0
);
"""


def _resolve(db_path: Path | None) -> Path:
    return db_path if db_path is not None else _DEFAULT_PATH


@contextmanager
def _conn(db_path: Path | None = None):
    db_path = _resolve(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    # Schema is idempotent (CREATE TABLE IF NOT EXISTS) so applying it on every
    # connection is a no-op when present and self-heals fresh / empty files.
    conn.executescript(_SCHEMA)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with _conn(db_path) as conn:
        conn.executescript(_SCHEMA)
    logger.info('live_db_initialised path=%s', db_path)


# Timestamp sanity floor: ms since epoch for 2023-11-14T22:13:20Z.
# Inserts with entry_ts_ms below this are rejected — guards against
# the historical test-data bug (id 1-10 had entry_ts_ms ∈ {0, 1}).
_MIN_VALID_TS_MS = 1_700_000_000_000


def insert_live_trade(trade: dict, db_path: Path | None = None) -> int:
    """Insert one closed live trade. Unknown keys flow into extra_json.

    Rejects rows with implausibly old timestamps (entry_ts_ms < 2023). Returns
    -1 if rejected — caller may log/alert but the bot keeps running.
    """
    entry_ts = trade.get('entry_ts_ms')
    if entry_ts is None or int(entry_ts) < _MIN_VALID_TS_MS:
        logger.error(
            'insert_live_trade.rejected_bad_timestamp entry_ts_ms=%s', entry_ts,
        )
        return -1
    known = {
        'entry_ts_ms', 'exit_ts_ms', 'side', 'entry_price', 'exit_price',
        'qty', 'notional_usdt', 'pnl_usdt', 'pnl_pct', 'result',
        'exit_reason', 'duration_min', 'session',
        'entry_rsi', 'entry_adx', 'entry_atr', 'entry_atr_pct',
        'entry_sma20', 'entry_sma50', 'mtf_15m_aligned',
        'htf_4h_trend', 'htf_daily_trend', 'regime', 'macro_event',
        'kelly_used',
    }
    payload = {k: trade.get(k) for k in known}
    extras = {k: v for k, v in trade.items() if k not in known}
    payload['extra_json'] = json.dumps(extras) if extras else None
    cols = ', '.join(payload.keys())
    placeholders = ', '.join(['?'] * len(payload))
    with _conn(db_path) as conn:
        cur = conn.execute(
            f'INSERT INTO live_trades ({cols}) VALUES ({placeholders})',
            tuple(payload.values()),
        )
        return int(cur.lastrowid)


def insert_near_miss(record: dict, db_path: Path | None = None) -> int:
    cols = (
        'ts_ms', 'reason', 'close', 'rsi', 'sma20',
        'rsi_distance', 'sma_distance_pct', 'side_intended',
    )
    values = tuple(record.get(c) for c in cols)
    with _conn(db_path) as conn:
        cur = conn.execute(
            f'INSERT INTO near_misses ({", ".join(cols)}) VALUES '
            f'({", ".join(["?"] * len(cols))})',
            values,
        )
        return int(cur.lastrowid)


def insert_kelly_change(record: dict, db_path: Path | None = None) -> int:
    cols = ('ts_ms', 'old_kelly_pct', 'new_kelly_pct',
            'rolling_win_rate', 'n_recent_trades', 'reason')
    values = tuple(record.get(c) for c in cols)
    with _conn(db_path) as conn:
        cur = conn.execute(
            f'INSERT INTO kelly_changes ({", ".join(cols)}) VALUES '
            f'({", ".join(["?"] * len(cols))})',
            values,
        )
        return int(cur.lastrowid)


def insert_shadow_trade(record: dict, db_path: Path | None = None) -> int:
    cols = ('decision_ts_ms', 'side', 'entry_price', 'sl_price', 'tp_price')
    values = tuple(record.get(c) for c in cols)
    with _conn(db_path) as conn:
        cur = conn.execute(
            f'INSERT INTO shadow_trades ({", ".join(cols)}) VALUES '
            f'({", ".join(["?"] * len(cols))})',
            values,
        )
        return int(cur.lastrowid)


def update_shadow_resolution(
    shadow_id: int, exit_ts_ms: int, exit_price: float,
    exit_reason: str, pnl_usdt: float, db_path: Path | None = None,
) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            'UPDATE shadow_trades SET exit_ts_ms=?, exit_price=?, '
            'exit_reason=?, pnl_usdt=?, resolved=1 WHERE id=?',
            (exit_ts_ms, exit_price, exit_reason, pnl_usdt, shadow_id),
        )


def list_live_trades(
    limit: int | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    with _conn(db_path) as conn:
        sql = 'SELECT * FROM live_trades ORDER BY exit_ts_ms DESC'
        if limit is not None:
            sql += f' LIMIT {int(limit)}'
        return [dict(r) for r in conn.execute(sql).fetchall()]


def list_near_misses(
    limit: int = 200, db_path: Path | None = None,
) -> list[dict]:
    with _conn(db_path) as conn:
        sql = f'SELECT * FROM near_misses ORDER BY ts_ms DESC LIMIT {int(limit)}'
        return [dict(r) for r in conn.execute(sql).fetchall()]


def list_kelly_changes(db_path: Path | None = None) -> list[dict]:
    with _conn(db_path) as conn:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM kelly_changes ORDER BY ts_ms DESC',
        ).fetchall()]


def query_all(sql: str, params: tuple = (),
              db_path: Path | None = None) -> list[dict]:
    """Read-only ad-hoc SELECT helper (rejects DDL/DML)."""
    if not sql.strip().lower().startswith('select'):
        raise ValueError('query_all only accepts SELECT statements')
    with _conn(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_trades(db_path: Path | None = None) -> int:
    with _conn(db_path) as conn:
        row = conn.execute('SELECT COUNT(*) AS n FROM live_trades').fetchone()
        return int(row['n'])
