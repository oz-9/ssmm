"""
P&L tracking database module.
SQLite with WAL mode for concurrent access.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "pnl.db"


def get_connection() -> sqlite3.Connection:
    """Get a database connection with WAL mode."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Initialize database schema."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fills (
                id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                price INTEGER NOT NULL,
                count INTEGER NOT NULL,
                is_taker BOOLEAN,
                fee_cost INTEGER,
                created_time TEXT NOT NULL,
                match_id TEXT,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fills_ticker ON fills(ticker);
            CREATE INDEX IF NOT EXISTS idx_fills_match_id ON fills(match_id);
            CREATE INDEX IF NOT EXISTS idx_fills_created_time ON fills(created_time);

            CREATE TABLE IF NOT EXISTS hedges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                side TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                odds REAL NOT NULL,
                outcome TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hedges_match_id ON hedges(match_id);

            CREATE TABLE IF NOT EXISTS pnl_matches (
                id TEXT PRIMARY KEY,
                ticker_a TEXT NOT NULL,
                ticker_b TEXT NOT NULL,
                theo_a INTEGER,
                theo_b INTEGER,
                event_time TEXT,
                settled_at TEXT,
                category TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pnl_matches_ticker_a ON pnl_matches(ticker_a);
            CREATE INDEX IF NOT EXISTS idx_pnl_matches_ticker_b ON pnl_matches(ticker_b);
        """)


def insert_fill(
    fill_id: str,
    ticker: str,
    side: str,
    action: str,
    price: int,
    count: int,
    is_taker: bool,
    fee_cost: int,
    created_time: str,
    match_id: Optional[str] = None,
) -> bool:
    """Insert or update a fill. Returns True if inserted, False if already exists."""
    with get_db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO fills (id, ticker, side, action, price, count, is_taker, fee_cost, created_time, match_id, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET match_id = excluded.match_id
                """,
                (fill_id, ticker, side, action, price, count, is_taker, fee_cost, created_time, match_id, datetime.utcnow().isoformat())
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_fills_for_match(match_id: str) -> list[dict]:
    """Get all fills for a match."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM fills WHERE match_id = ? ORDER BY created_time",
            (match_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_fills_by_ticker(ticker: str) -> list[dict]:
    """Get all fills for a ticker."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM fills WHERE ticker = ? ORDER BY created_time",
            (ticker,)
        ).fetchall()
        return [dict(row) for row in rows]


def link_fills_to_match(match_id: str, ticker_a: str, ticker_b: str):
    """Link unlinked fills to a match by ticker."""
    with get_db() as conn:
        conn.execute(
            """
            UPDATE fills SET match_id = ?
            WHERE match_id IS NULL AND ticker IN (?, ?)
            """,
            (match_id, ticker_a, ticker_b)
        )
