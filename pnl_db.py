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


def insert_hedge(
    match_id: str,
    platform: str,
    side: str,
    amount_usd: float,
    odds: float,
) -> int:
    """Insert a hedge. Returns the hedge ID."""
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO hedges (match_id, platform, side, amount_usd, odds, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (match_id, platform, side, amount_usd, odds, datetime.utcnow().isoformat())
        )
        return cursor.lastrowid


def update_hedge_outcome(hedge_id: int, outcome: str) -> bool:
    """Update hedge outcome ('win', 'loss', 'push'). Returns True if updated."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE hedges SET outcome = ? WHERE id = ?",
            (outcome, hedge_id)
        )
        return cursor.rowcount > 0


def get_hedges_for_match(match_id: str) -> list[dict]:
    """Get all hedges for a match."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM hedges WHERE match_id = ? ORDER BY created_at",
            (match_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def delete_hedge(hedge_id: int) -> bool:
    """Delete a hedge. Returns True if deleted."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM hedges WHERE id = ?", (hedge_id,))
        return cursor.rowcount > 0


def upsert_match(
    match_id: str,
    ticker_a: str,
    ticker_b: str,
    theo_a: Optional[int] = None,
    theo_b: Optional[int] = None,
    event_time: Optional[str] = None,
    category: Optional[str] = None,
):
    """Insert or update a match."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO pnl_matches (id, ticker_a, ticker_b, theo_a, theo_b, event_time, category)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                theo_a = COALESCE(excluded.theo_a, pnl_matches.theo_a),
                theo_b = COALESCE(excluded.theo_b, pnl_matches.theo_b),
                event_time = COALESCE(excluded.event_time, pnl_matches.event_time),
                category = COALESCE(excluded.category, pnl_matches.category)
            """,
            (match_id, ticker_a, ticker_b, theo_a, theo_b, event_time, category)
        )


def mark_match_settled(match_id: str):
    """Mark a match as settled."""
    with get_db() as conn:
        conn.execute(
            "UPDATE pnl_matches SET settled_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), match_id)
        )


def get_match(match_id: str) -> Optional[dict]:
    """Get a match by ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM pnl_matches WHERE id = ?",
            (match_id,)
        ).fetchone()
        return dict(row) if row else None


def get_all_matches() -> list[dict]:
    """Get all matches."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM pnl_matches ORDER BY event_time DESC").fetchall()
        return [dict(row) for row in rows]


def calculate_match_pnl(match_id: str, theo_a: int, theo_b: int) -> dict:
    """
    Calculate P&L breakdown for a match.

    Returns:
        {
            "arb_profit": int,        # cents from completed pairs
            "arb_pairs": int,         # number of paired contracts
            "leftover_a": int,        # unpaired contracts long A
            "leftover_b": int,        # unpaired contracts long B
            "leftover_cost_a": int,   # cost basis for leftover A
            "leftover_cost_b": int,   # cost basis for leftover B
            "leftover_ev": int,       # theoretical EV in cents
            "fees": int,              # total fees in cents
            "hedge_pnl": float,       # hedge P&L in USD
            "fills_a": list,          # fills going long A
            "fills_b": list,          # fills going long B
        }
    """
    fills = get_fills_for_match(match_id)
    hedges = get_hedges_for_match(match_id)

    # Separate fills by direction
    # Long A = buy YES on ticker_a OR buy NO on ticker_b
    # Long B = buy YES on ticker_b OR buy NO on ticker_a
    fills_a = []  # (price, count, fee)
    fills_b = []

    match = get_match(match_id)
    if not match:
        return {"error": "Match not found"}

    ticker_a = match["ticker_a"]
    ticker_b = match["ticker_b"]

    for f in fills:
        fee = f["fee_cost"] or 0
        if (f["ticker"] == ticker_a and f["side"] == "yes") or \
           (f["ticker"] == ticker_b and f["side"] == "no"):
            fills_a.append({"price": f["price"], "count": f["count"], "fee": fee})
        else:
            fills_b.append({"price": f["price"], "count": f["count"], "fee": fee})

    # Pair fills FIFO
    total_a = sum(f["count"] for f in fills_a)
    total_b = sum(f["count"] for f in fills_b)
    paired = min(total_a, total_b)

    # Calculate arb profit from paired contracts
    arb_profit = 0
    cost_a = 0
    cost_b = 0
    remaining_a = paired
    remaining_b = paired

    # Sum costs for paired portion
    for f in fills_a:
        take = min(f["count"], remaining_a)
        cost_a += take * f["price"]
        remaining_a -= take
        if remaining_a == 0:
            break

    for f in fills_b:
        take = min(f["count"], remaining_b)
        cost_b += take * f["price"]
        remaining_b -= take
        if remaining_b == 0:
            break

    arb_profit = (100 * paired) - cost_a - cost_b

    # Calculate leftover
    leftover_a = total_a - paired
    leftover_b = total_b - paired

    # Leftover cost basis
    leftover_cost_a = 0
    leftover_cost_b = 0
    skip_a = paired
    skip_b = paired

    for f in fills_a:
        if skip_a >= f["count"]:
            skip_a -= f["count"]
        else:
            take = f["count"] - skip_a
            leftover_cost_a += take * f["price"]
            skip_a = 0

    for f in fills_b:
        if skip_b >= f["count"]:
            skip_b -= f["count"]
        else:
            take = f["count"] - skip_b
            leftover_cost_b += take * f["price"]
            skip_b = 0

    # Leftover theoretical EV
    leftover_ev = int(leftover_a * theo_a + leftover_b * theo_b)

    # Total fees
    fees = sum(f["fee"] for f in fills_a) + sum(f["fee"] for f in fills_b)

    # Hedge P&L
    hedge_pnl = 0.0
    for h in hedges:
        if h["outcome"] == "win":
            hedge_pnl += h["amount_usd"] * (h["odds"] - 1)
        elif h["outcome"] == "loss":
            hedge_pnl -= h["amount_usd"]
        # push = 0

    return {
        "arb_profit": arb_profit,
        "arb_pairs": paired,
        "leftover_a": leftover_a,
        "leftover_b": leftover_b,
        "leftover_cost_a": leftover_cost_a,
        "leftover_cost_b": leftover_cost_b,
        "leftover_ev": leftover_ev,
        "fees": fees,
        "hedge_pnl": hedge_pnl,
        "total_fills_a": total_a,
        "total_fills_b": total_b,
    }
