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
                result_a TEXT,
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


def mark_match_settled(match_id: str, result_a: str = None):
    """Mark a match as settled with result ('yes' or 'no' for ticker_a)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE pnl_matches SET settled_at = ?, result_a = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), result_a, match_id)
        )


def update_match_result(match_id: str, result_a: str):
    """Update match result ('yes' = ticker_a won, 'no' = ticker_b won)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE pnl_matches SET result_a = ?, settled_at = COALESCE(settled_at, ?) WHERE id = ?",
            (result_a, datetime.utcnow().isoformat(), match_id)
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


def calculate_match_pnl(match_id: str, theo_a: int = None, theo_b: int = None,
                        get_mid_price: callable = None) -> dict:
    """
    Calculate P&L breakdown for a match.

    Args:
        match_id: The match ID
        theo_a: Theo for side A (optional, uses stored value)
        theo_b: Theo for side B (optional, uses stored value)
        get_mid_price: Optional callback(ticker) -> int that returns mid price in cents.
                       Used to calculate AV for open positions based on market value.

    Returns:
        {
            "settled": bool,      # whether match has settled
            "arb": int,           # guaranteed profit from paired contracts (cents)
            "ev": int,            # expected profit from leftover (cents)
            "av": int,            # actual/market value profit from leftover (cents)
            "delta": int,         # av - ev (cents)
            "hedge": float,       # hedge P&L (USD)
            "fees": int,          # total fees (cents)
            "pnl": float,         # net = arb + av + hedge - fees (USD)
            "pairs": int,         # number of paired contracts
            "leftover_a": int,    # unpaired contracts long A
            "leftover_b": int,    # unpaired contracts long B
        }
    """
    fills = get_fills_for_match(match_id)
    hedges = get_hedges_for_match(match_id)

    match = get_match(match_id)
    if not match:
        return {"error": "Match not found"}

    ticker_a = match["ticker_a"]
    ticker_b = match["ticker_b"]
    result_a = match.get("result_a")  # 'yes' or 'no' or None

    # Use stored theo or default to 50
    if theo_a is None:
        theo_a = match.get("theo_a") or 50
    if theo_b is None:
        theo_b = match.get("theo_b") or 50

    # Separate fills by direction
    # Long A = buy YES on ticker_a OR buy NO on ticker_b
    # Long B = buy YES on ticker_b OR buy NO on ticker_a
    fills_a = []
    fills_b = []
    total_fees = 0

    for f in fills:
        total_fees += f["fee_cost"] or 0
        entry = {"price": f["price"], "count": f["count"], "ticker": f["ticker"], "side": f["side"]}
        if (f["ticker"] == ticker_a and f["side"] == "yes") or \
           (f["ticker"] == ticker_b and f["side"] == "no"):
            fills_a.append(entry)
        else:
            fills_b.append(entry)

    total_a = sum(f["count"] for f in fills_a)
    total_b = sum(f["count"] for f in fills_b)
    pairs = min(total_a, total_b)

    # ARB: profit from paired contracts (FIFO)
    cost_a_paired = 0
    remaining = pairs
    for f in fills_a:
        take = min(f["count"], remaining)
        cost_a_paired += take * f["price"]
        remaining -= take
        if remaining == 0:
            break

    cost_b_paired = 0
    remaining = pairs
    for f in fills_b:
        take = min(f["count"], remaining)
        cost_b_paired += take * f["price"]
        remaining -= take
        if remaining == 0:
            break

    arb = (100 * pairs) - cost_a_paired - cost_b_paired

    # LEFTOVER: unpaired contracts
    leftover_a = total_a - pairs
    leftover_b = total_b - pairs

    # Leftover cost (FIFO - skip paired portion)
    leftover_cost_a = 0
    skip = pairs
    for f in fills_a:
        if skip >= f["count"]:
            skip -= f["count"]
        else:
            take = f["count"] - skip
            leftover_cost_a += take * f["price"]
            skip = 0

    leftover_cost_b = 0
    skip = pairs
    for f in fills_b:
        if skip >= f["count"]:
            skip -= f["count"]
        else:
            take = f["count"] - skip
            leftover_cost_b += take * f["price"]
            skip = 0

    # EV = (theo - cost/count) * count = theo*count - cost
    ev = (theo_a * leftover_a - leftover_cost_a) + (theo_b * leftover_b - leftover_cost_b)

    # AV = actual/market value - cost for leftover
    if result_a:
        # Settled: use actual payout
        if result_a == "yes":
            payout_a = 100 * leftover_a
            payout_b = 0
        else:
            payout_a = 0
            payout_b = 100 * leftover_b
        av = (payout_a - leftover_cost_a) + (payout_b - leftover_cost_b)
    elif get_mid_price and (leftover_a > 0 or leftover_b > 0):
        # Open: use market value (mid price)
        try:
            mid_a = get_mid_price(ticker_a) if leftover_a > 0 else 0
            mid_b = get_mid_price(ticker_b) if leftover_b > 0 else 0
            market_value_a = mid_a * leftover_a
            market_value_b = mid_b * leftover_b
            av = (market_value_a - leftover_cost_a) + (market_value_b - leftover_cost_b)
        except:
            av = 0  # Fallback if price fetch fails
    else:
        av = 0

    delta = av - ev

    # Hedge P&L
    hedge = 0.0
    for h in hedges:
        if h["outcome"] == "win":
            hedge += h["amount_usd"] * (h["odds"] - 1)
        elif h["outcome"] == "loss":
            hedge -= h["amount_usd"]

    # Net PnL = arb + av + hedge - fees (in USD)
    pnl = arb / 100 + av / 100 + hedge - total_fees / 100

    return {
        "settled": result_a is not None,
        "arb": arb,
        "ev": ev,
        "av": av,
        "delta": delta,
        "hedge": hedge,
        "fees": total_fees,
        "pnl": pnl,
        "pairs": pairs,
        "leftover_a": leftover_a,
        "leftover_b": leftover_b,
    }


def get_pnl_summary(period: str = "daily", get_mid_price: callable = None) -> list[dict]:
    """
    Get aggregated P&L by period, calculated per-fill per-day.

    Args:
        period: 'daily', 'weekly', or 'monthly'
        get_mid_price: Optional callback(ticker) -> int for market value of open positions

    Returns list of:
        {
            "period": str,
            "arb": float,       # arb profit credited when pairs complete (USD)
            "ev": float,        # expected profit from fills that day (USD)
            "av": float,        # actual/market value from fills that day (USD)
            "delta": float,     # av - ev (USD)
            "hedge": float,     # hedge P&L (USD)
            "fees": float,      # fees from fills that day (USD)
            "pnl": float,       # net = arb + av + hedge - fees (USD)
        }
    """
    from collections import defaultdict

    def get_period_key(created_time: str) -> str:
        try:
            dt = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
            if period == "daily":
                return dt.strftime("%Y-%m-%d")
            elif period == "weekly":
                return f"{dt.year}-W{dt.isocalendar()[1]:02d}"
            else:  # monthly
                return dt.strftime("%Y-%m")
        except:
            return "unknown"

    periods = defaultdict(lambda: {
        "arb": 0, "ev": 0, "av": 0, "hedge": 0.0, "fees": 0,
    })

    # Process each match
    matches = get_all_matches()
    for m in matches:
        match_id = m["id"]
        ticker_a = m["ticker_a"]
        ticker_b = m["ticker_b"]
        theo_a = m.get("theo_a") or 50
        theo_b = m.get("theo_b") or 50
        result_a = m.get("result_a")

        fills = get_fills_for_match(match_id)
        if not fills:
            continue

        # Get mid prices for open positions
        mid_a = None
        mid_b = None
        if not result_a and get_mid_price:
            try:
                mid_a = get_mid_price(ticker_a)
                mid_b = get_mid_price(ticker_b)
            except:
                pass

        # Separate fills by direction with their dates
        # Long A = buy YES on ticker_a OR buy NO on ticker_b
        fills_a = []  # [(period_key, price, count, fee)]
        fills_b = []

        for f in fills:
            key = get_period_key(f["created_time"])
            fee = f["fee_cost"] or 0
            entry = (key, f["price"], f["count"], fee)

            if (f["ticker"] == ticker_a and f["side"] == "yes") or \
               (f["ticker"] == ticker_b and f["side"] == "no"):
                fills_a.append(entry)
            else:
                fills_b.append(entry)

        # Process fills FIFO to determine pairing
        # Arb is credited on the day the SECOND leg completes the pair
        idx_a = 0
        idx_b = 0
        remaining_a = fills_a[0][2] if fills_a else 0
        remaining_b = fills_b[0][2] if fills_b else 0

        while idx_a < len(fills_a) and idx_b < len(fills_b):
            key_a, price_a, _, _ = fills_a[idx_a]
            key_b, price_b, _, _ = fills_b[idx_b]

            # Pair as many as possible
            pair_count = min(remaining_a, remaining_b)
            arb_profit = (100 - price_a - price_b) * pair_count

            # Credit arb to the LATER date (when pair completed)
            arb_key = max(key_a, key_b)
            periods[arb_key]["arb"] += arb_profit

            remaining_a -= pair_count
            remaining_b -= pair_count

            if remaining_a == 0:
                idx_a += 1
                if idx_a < len(fills_a):
                    remaining_a = fills_a[idx_a][2]

            if remaining_b == 0:
                idx_b += 1
                if idx_b < len(fills_b):
                    remaining_b = fills_b[idx_b][2]

        # Process remaining fills as leftover (ev/av per fill date)
        # First, rebuild remaining counts after pairing
        paired_a = sum(f[2] for f in fills_a) - remaining_a - sum(fills_a[i][2] for i in range(idx_a + 1, len(fills_a))) if fills_a else 0
        paired_b = sum(f[2] for f in fills_b) - remaining_b - sum(fills_b[i][2] for i in range(idx_b + 1, len(fills_b))) if fills_b else 0

        # Process all fills for ev/av (leftover portion only)
        def process_leftover(fills_list, theo, mid_price, is_a_side):
            skip = paired_a if is_a_side else paired_b
            for key, price, count, fee in fills_list:
                # Add fees for this fill
                periods[key]["fees"] += fee

                # Determine leftover count for this fill
                if skip >= count:
                    skip -= count
                    continue
                leftover_count = count - skip
                skip = 0

                # EV = (theo - price) * leftover_count
                ev = (theo - price) * leftover_count
                periods[key]["ev"] += ev

                # AV = actual or market value
                if result_a is not None:
                    # Settled
                    if is_a_side:
                        won = (result_a == "yes")
                    else:
                        won = (result_a == "no")
                    payout = 100 * leftover_count if won else 0
                    av = payout - price * leftover_count
                elif mid_price:
                    # Open with market value
                    market_value = mid_price * leftover_count
                    av = market_value - price * leftover_count
                else:
                    av = 0
                periods[key]["av"] += av

        process_leftover(fills_a, theo_a, mid_a, True)
        process_leftover(fills_b, theo_b, mid_b, False)

        # Add hedge P&L to the first fill's period
        hedges = get_hedges_for_match(match_id)
        if hedges and fills:
            hedge_key = get_period_key(fills[0]["created_time"])
            for h in hedges:
                if h["outcome"] == "win":
                    periods[hedge_key]["hedge"] += h["amount_usd"] * (h["odds"] - 1)
                elif h["outcome"] == "loss":
                    periods[hedge_key]["hedge"] -= h["amount_usd"]

    # Convert to list
    result = []
    for key in sorted(periods.keys(), reverse=True):
        p = periods[key]
        arb = p["arb"] / 100
        ev = p["ev"] / 100
        av = p["av"] / 100
        delta = av - ev
        hedge = p["hedge"]
        fees = p["fees"] / 100
        pnl = arb + av + hedge - fees
        result.append({
            "period": key,
            "arb": arb,
            "ev": ev,
            "av": av,
            "delta": delta,
            "hedge": hedge,
            "fees": fees,
            "pnl": pnl,
        })

    return result


def get_open_positions() -> list[dict]:
    """Get all unsettled matches with their EV."""
    matches = get_all_matches()
    result = []

    for m in matches:
        pnl_data = calculate_match_pnl(m["id"])
        if "error" in pnl_data:
            continue
        if pnl_data.get("settled"):
            continue  # Skip settled matches

        leftover = pnl_data.get("leftover_a", 0) + pnl_data.get("leftover_b", 0)
        if leftover == 0 and pnl_data.get("pairs", 0) == 0:
            continue  # No position

        result.append({
            "match_id": m["id"],
            "ticker_a": m["ticker_a"],
            "ticker_b": m["ticker_b"],
            "arb": pnl_data["arb"] / 100,
            "ev": pnl_data["ev"] / 100,
            "pairs": pnl_data["pairs"],
            "leftover_a": pnl_data["leftover_a"],
            "leftover_b": pnl_data["leftover_b"],
            "fees": pnl_data["fees"] / 100,
        })

    return result


def get_total_pnl(get_mid_price: callable = None) -> dict:
    """Get total P&L across all matches (including open with market value)."""
    matches = get_all_matches()

    totals = {"arb": 0, "ev": 0, "av": 0, "hedge": 0.0, "fees": 0}

    for m in matches:
        pnl_data = calculate_match_pnl(m["id"], get_mid_price=get_mid_price)
        if "error" in pnl_data:
            continue

        totals["arb"] += pnl_data["arb"]
        totals["ev"] += pnl_data["ev"]
        totals["av"] += pnl_data["av"]
        totals["hedge"] += pnl_data["hedge"]
        totals["fees"] += pnl_data["fees"]

    arb = totals["arb"] / 100
    ev = totals["ev"] / 100
    av = totals["av"] / 100
    delta = av - ev
    hedge = totals["hedge"]
    fees = totals["fees"] / 100
    pnl = arb + av + hedge - fees

    return {
        "arb": arb,
        "ev": ev,
        "av": av,
        "delta": delta,
        "hedge": hedge,
        "fees": fees,
        "pnl": pnl,
    }
