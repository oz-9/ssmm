# PnL Tracking Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Track realized and unrealized P&L for the Kalshi market-making dashboard with SQLite persistence.

**Architecture:** SQLite database (`pnl.db`) stores fills, hedges, and match metadata. Dashboard syncs fills on startup and when WebSocket reports fills. API endpoints expose P&L calculations. Frontend shows per-match and aggregated P&L.

**Tech Stack:** Python 3.11+, SQLite (WAL mode), FastAPI, Pydantic

---

## Task 1: Create pnl_db.py with Schema Initialization

**Files:**
- Create: `pnl_db.py`

**Step 1: Create database module with schema**

```python
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
```

**Step 2: Commit**

```bash
git add pnl_db.py
git commit -m "feat(pnl): add database module with schema initialization"
```

---

## Task 2: Add Fill CRUD Operations

**Files:**
- Modify: `pnl_db.py`

**Step 1: Add insert_fill function**

Add after `init_db()`:

```python
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
```

**Step 2: Commit**

```bash
git add pnl_db.py
git commit -m "feat(pnl): add fill CRUD operations"
```

---

## Task 3: Add Hedge CRUD Operations

**Files:**
- Modify: `pnl_db.py`

**Step 1: Add hedge functions**

Add after fill functions:

```python
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
```

**Step 2: Commit**

```bash
git add pnl_db.py
git commit -m "feat(pnl): add hedge CRUD operations"
```

---

## Task 4: Add Match Persistence

**Files:**
- Modify: `pnl_db.py`

**Step 1: Add match functions**

Add after hedge functions:

```python
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
```

**Step 2: Commit**

```bash
git add pnl_db.py
git commit -m "feat(pnl): add match persistence functions"
```

---

## Task 5: Add P&L Calculation Functions

**Files:**
- Modify: `pnl_db.py`

**Step 1: Add P&L calculation**

Add after match functions:

```python
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
```

**Step 2: Commit**

```bash
git add pnl_db.py
git commit -m "feat(pnl): add P&L calculation function"
```

---

## Task 6: Add Aggregation Functions

**Files:**
- Modify: `pnl_db.py`

**Step 1: Add summary aggregation**

Add after `calculate_match_pnl`:

```python
def get_pnl_summary(period: str = "daily") -> list[dict]:
    """
    Get aggregated P&L by period.

    Args:
        period: 'daily', 'weekly', or 'monthly'

    Returns list of:
        {
            "period": str,          # date/week/month label
            "arb_profit": int,
            "leftover_ev": int,
            "fees": int,
            "hedge_pnl": float,
            "net_profit": float,    # arb_profit - fees + hedge_pnl (excludes leftover until settled)
        }
    """
    with get_db() as conn:
        # Get all matches with their P&L
        matches = conn.execute("""
            SELECT m.id, m.ticker_a, m.ticker_b, m.theo_a, m.theo_b, m.event_time
            FROM pnl_matches m
            WHERE EXISTS (SELECT 1 FROM fills f WHERE f.match_id = m.id)
            ORDER BY m.event_time
        """).fetchall()

    # Group by period
    from collections import defaultdict
    periods = defaultdict(lambda: {
        "arb_profit": 0,
        "leftover_ev": 0,
        "fees": 0,
        "hedge_pnl": 0.0,
    })

    for m in matches:
        match_id = m["id"]
        theo_a = m["theo_a"] or 50
        theo_b = m["theo_b"] or 50
        event_time = m["event_time"] or ""

        pnl = calculate_match_pnl(match_id, theo_a, theo_b)

        # Determine period key
        if event_time:
            try:
                dt = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                if period == "daily":
                    key = dt.strftime("%Y-%m-%d")
                elif period == "weekly":
                    key = f"{dt.year}-W{dt.isocalendar()[1]:02d}"
                else:  # monthly
                    key = dt.strftime("%Y-%m")
            except:
                key = "unknown"
        else:
            key = "unknown"

        periods[key]["arb_profit"] += pnl.get("arb_profit", 0)
        periods[key]["leftover_ev"] += pnl.get("leftover_ev", 0)
        periods[key]["fees"] += pnl.get("fees", 0)
        periods[key]["hedge_pnl"] += pnl.get("hedge_pnl", 0.0)

    # Convert to list with net profit
    result = []
    for key in sorted(periods.keys(), reverse=True):
        p = periods[key]
        result.append({
            "period": key,
            "arb_profit": p["arb_profit"],
            "leftover_ev": p["leftover_ev"],
            "fees": p["fees"],
            "hedge_pnl": p["hedge_pnl"],
            "net_profit": p["arb_profit"] - p["fees"] + p["hedge_pnl"],
        })

    return result
```

**Step 2: Commit**

```bash
git add pnl_db.py
git commit -m "feat(pnl): add aggregation functions"
```

---

## Task 7: Integrate Database Init in Dashboard

**Files:**
- Modify: `dashboard.py:7-8` (imports)
- Modify: `dashboard.py:1201-1214` (lifespan)

**Step 1: Add import**

Add after line 8 (`import json`):

```python
import pnl_db
```

**Step 2: Initialize database in lifespan**

In the `lifespan` function, after `order_locks_lock = asyncio.Lock()` (around line 1207), add:

```python
    # Init P&L database
    pnl_db.init_db()
```

**Step 3: Commit**

```bash
git add dashboard.py
git commit -m "feat(pnl): initialize database on startup"
```

---

## Task 8: Save Fills to Database on WebSocket Fill

**Files:**
- Modify: `dashboard.py:657-709` (on_fill function)

**Step 1: Update on_fill to persist**

In the `on_fill` function, after recording the fill (around line 696), add:

```python
        # Persist fill to database
        pnl_db.insert_fill(
            fill_id=fill_data.get("trade_id", f"{ticker}_{side}_{fill_time}"),
            ticker=ticker,
            side=side,
            action="buy",
            price=price,
            count=count,
            is_taker=fill_data.get("is_taker", True),
            fee_cost=fill_data.get("taker_fee", 0),
            created_time=datetime.datetime.utcnow().isoformat(),
            match_id=match.id,
        )
```

**Step 2: Commit**

```bash
git add dashboard.py
git commit -m "feat(pnl): persist fills from WebSocket to database"
```

---

## Task 9: Save Match to Database on Add

**Files:**
- Modify: `dashboard.py:374-425` (add_match function)

**Step 1: Update add_match to persist**

At the end of `add_match`, before `return match` (around line 425), add:

```python
    # Persist match to P&L database
    pnl_db.upsert_match(
        match_id=match.id,
        ticker_a=config.ticker_a,
        ticker_b=config.ticker_b,
        theo_a=int(match.market_a.theo),
        theo_b=int(match.market_b.theo),
        event_time=match.event_time.isoformat() if match.event_time else None,
        category=match.category,
    )
    # Link any existing fills for these tickers
    pnl_db.link_fills_to_match(match.id, config.ticker_a, config.ticker_b)
```

**Step 2: Commit**

```bash
git add dashboard.py
git commit -m "feat(pnl): persist matches and link fills on add"
```

---

## Task 10: Add P&L API Endpoints

**Files:**
- Modify: `dashboard.py` (add after `/api/kill` endpoint, around line 1446)

**Step 1: Add Pydantic models**

Add before the endpoints:

```python
class HedgeCreate(BaseModel):
    """Create a hedge."""
    match_id: str
    platform: str
    side: str  # 'team_a' or 'team_b'
    amount_usd: float
    odds: float

class HedgeUpdate(BaseModel):
    """Update hedge outcome."""
    outcome: str  # 'win', 'loss', 'push'
```

**Step 2: Add API endpoints**

Add after `/api/kill` endpoint:

```python
@app.get("/api/pnl/match/{match_id}")
async def api_get_match_pnl(match_id: str):
    """Get P&L breakdown for a match."""
    if match_id not in matches:
        return {"error": "Match not found in active matches"}

    match = matches[match_id]
    theo_a = int(match.market_a.theo)
    theo_b = int(match.market_b.theo)

    pnl = pnl_db.calculate_match_pnl(match_id, theo_a, theo_b)
    return pnl


@app.get("/api/pnl/summary")
async def api_get_pnl_summary(period: str = "daily"):
    """Get aggregated P&L summary."""
    if period not in ("daily", "weekly", "monthly"):
        return {"error": "Invalid period. Use 'daily', 'weekly', or 'monthly'"}
    return {"summary": pnl_db.get_pnl_summary(period)}


@app.post("/api/hedges")
async def api_create_hedge(hedge: HedgeCreate):
    """Create a hedge entry."""
    hedge_id = pnl_db.insert_hedge(
        match_id=hedge.match_id,
        platform=hedge.platform,
        side=hedge.side,
        amount_usd=hedge.amount_usd,
        odds=hedge.odds,
    )
    return {"id": hedge_id}


@app.put("/api/hedges/{hedge_id}")
async def api_update_hedge(hedge_id: int, update: HedgeUpdate):
    """Update hedge outcome."""
    if update.outcome not in ("win", "loss", "push"):
        return {"error": "Invalid outcome. Use 'win', 'loss', or 'push'"}
    success = pnl_db.update_hedge_outcome(hedge_id, update.outcome)
    return {"ok": success}


@app.get("/api/hedges")
async def api_get_hedges(match_id: Optional[str] = None):
    """Get hedges, optionally filtered by match."""
    if match_id:
        return {"hedges": pnl_db.get_hedges_for_match(match_id)}
    # Return all hedges
    with pnl_db.get_db() as conn:
        rows = conn.execute("SELECT * FROM hedges ORDER BY created_at DESC").fetchall()
        return {"hedges": [dict(row) for row in rows]}


@app.delete("/api/hedges/{hedge_id}")
async def api_delete_hedge(hedge_id: int):
    """Delete a hedge."""
    success = pnl_db.delete_hedge(hedge_id)
    return {"ok": success}
```

**Step 3: Commit**

```bash
git add dashboard.py
git commit -m "feat(pnl): add P&L and hedge API endpoints"
```

---

## Task 11: Add P&L to Match State

**Files:**
- Modify: `dashboard.py:744-794` (get_state function)

**Step 1: Add P&L to match state**

In `get_state()`, inside the match dict comprehension (around line 784), add after `"event_time"`:

```python
                "pnl": pnl_db.calculate_match_pnl(
                    m.id,
                    int(m.market_a.theo),
                    int(m.market_b.theo)
                ) if pnl_db.get_match(m.id) else None,
```

**Step 2: Commit**

```bash
git add dashboard.py
git commit -m "feat(pnl): include P&L in match state broadcast"
```

---

## Task 12: Add Frontend P&L Display

**Files:**
- Modify: `static/index.html`

**Step 1: Add P&L section to match card**

Find the match card template in the JavaScript and add P&L display after the orders section. In the `renderMatches` function, add after the order status display:

```javascript
// P&L display (if available)
let pnlHtml = '';
if (m.pnl && (m.pnl.arb_profit !== 0 || m.pnl.leftover_a > 0 || m.pnl.leftover_b > 0)) {
    const arbProfit = (m.pnl.arb_profit / 100).toFixed(2);
    const fees = (m.pnl.fees / 100).toFixed(2);
    const netArb = ((m.pnl.arb_profit - m.pnl.fees) / 100).toFixed(2);
    const leftoverEv = (m.pnl.leftover_ev / 100).toFixed(2);

    pnlHtml = `
        <div class="pnl-section" style="margin-top: 8px; padding-top: 8px; border-top: 1px solid #333; font-size: 11px;">
            <div style="display: flex; justify-content: space-between; color: #888;">
                <span>Arb: <span style="color: ${m.pnl.arb_profit >= 0 ? '#4ade80' : '#f87171'}">$${arbProfit}</span></span>
                <span>Fees: <span style="color: #f87171">-$${fees}</span></span>
                <span>Net: <span style="color: ${parseFloat(netArb) >= 0 ? '#4ade80' : '#f87171'}">$${netArb}</span></span>
            </div>
            ${m.pnl.leftover_a > 0 || m.pnl.leftover_b > 0 ? `
            <div style="color: #888; margin-top: 4px;">
                Leftover: ${m.pnl.leftover_a} A / ${m.pnl.leftover_b} B (EV: $${leftoverEv})
            </div>
            ` : ''}
        </div>
    `;
}
```

Then include `${pnlHtml}` in the card HTML.

**Step 2: Commit**

```bash
git add static/index.html
git commit -m "feat(pnl): add P&L display to match cards"
```

---

## Task 13: Add Hedge Entry Modal

**Files:**
- Modify: `static/index.html`

**Step 1: Add hedge modal HTML**

Add modal HTML (before closing `</body>`):

```html
<!-- Hedge Modal -->
<div id="hedge-modal" class="modal" style="display: none;">
    <div class="modal-content" style="background: #1a1a1a; padding: 20px; border-radius: 8px; max-width: 400px; margin: 100px auto;">
        <h3 style="margin-top: 0;">Add Hedge</h3>
        <form id="hedge-form">
            <input type="hidden" id="hedge-match-id">
            <div style="margin-bottom: 12px;">
                <label style="display: block; margin-bottom: 4px;">Platform</label>
                <select id="hedge-platform" style="width: 100%; padding: 8px; background: #333; border: 1px solid #555; color: #fff; border-radius: 4px;">
                    <option value="thunderpick">Thunderpick</option>
                    <option value="ggbet">GGBet</option>
                    <option value="pinnacle">Pinnacle</option>
                    <option value="other">Other</option>
                </select>
            </div>
            <div style="margin-bottom: 12px;">
                <label style="display: block; margin-bottom: 4px;">Side</label>
                <select id="hedge-side" style="width: 100%; padding: 8px; background: #333; border: 1px solid #555; color: #fff; border-radius: 4px;">
                    <option value="team_a">Team A</option>
                    <option value="team_b">Team B</option>
                </select>
            </div>
            <div style="margin-bottom: 12px;">
                <label style="display: block; margin-bottom: 4px;">Amount (USD)</label>
                <input type="number" id="hedge-amount" step="0.01" min="0" style="width: 100%; padding: 8px; background: #333; border: 1px solid #555; color: #fff; border-radius: 4px;">
            </div>
            <div style="margin-bottom: 12px;">
                <label style="display: block; margin-bottom: 4px;">Odds (decimal)</label>
                <input type="number" id="hedge-odds" step="0.01" min="1" style="width: 100%; padding: 8px; background: #333; border: 1px solid #555; color: #fff; border-radius: 4px;">
            </div>
            <div style="display: flex; gap: 8px; justify-content: flex-end;">
                <button type="button" onclick="closeHedgeModal()" style="padding: 8px 16px; background: #555; border: none; color: #fff; border-radius: 4px; cursor: pointer;">Cancel</button>
                <button type="submit" style="padding: 8px 16px; background: #3b82f6; border: none; color: #fff; border-radius: 4px; cursor: pointer;">Add Hedge</button>
            </div>
        </form>
    </div>
</div>
```

**Step 2: Add modal CSS**

```css
.modal {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0,0,0,0.7);
    z-index: 1000;
}
```

**Step 3: Add modal JavaScript**

```javascript
function openHedgeModal(matchId) {
    document.getElementById('hedge-match-id').value = matchId;
    document.getElementById('hedge-modal').style.display = 'block';
}

function closeHedgeModal() {
    document.getElementById('hedge-modal').style.display = 'none';
    document.getElementById('hedge-form').reset();
}

document.getElementById('hedge-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const data = {
        match_id: document.getElementById('hedge-match-id').value,
        platform: document.getElementById('hedge-platform').value,
        side: document.getElementById('hedge-side').value,
        amount_usd: parseFloat(document.getElementById('hedge-amount').value),
        odds: parseFloat(document.getElementById('hedge-odds').value),
    };
    await fetch('/api/hedges', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    closeHedgeModal();
});
```

**Step 4: Add hedge button to match card**

Add a hedge button in the match card template:

```javascript
<button onclick="openHedgeModal('${m.id}')" style="padding: 4px 8px; font-size: 10px; background: #6366f1; border: none; color: white; border-radius: 4px; cursor: pointer;">+ Hedge</button>
```

**Step 5: Commit**

```bash
git add static/index.html
git commit -m "feat(pnl): add hedge entry modal"
```

---

## Task 14: Add P&L Summary View

**Files:**
- Modify: `static/index.html`

**Step 1: Add summary section**

Add a collapsible P&L summary section (similar to category sections):

```html
<div class="category-section" id="pnl-summary-section">
    <div class="category-header" onclick="toggleCategory('pnl-summary')">
        <span class="category-arrow" id="pnl-summary-arrow">▼</span>
        <span class="category-title">P&L Summary</span>
    </div>
    <div class="category-content" id="pnl-summary-content">
        <div style="padding: 12px;">
            <select id="pnl-period" onchange="loadPnlSummary()" style="margin-bottom: 12px; padding: 6px; background: #333; border: 1px solid #555; color: #fff; border-radius: 4px;">
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
            </select>
            <div id="pnl-summary-table"></div>
        </div>
    </div>
</div>
```

**Step 2: Add summary loading function**

```javascript
async function loadPnlSummary() {
    const period = document.getElementById('pnl-period').value;
    const resp = await fetch(`/api/pnl/summary?period=${period}`);
    const data = await resp.json();

    if (!data.summary || data.summary.length === 0) {
        document.getElementById('pnl-summary-table').innerHTML = '<p style="color: #888;">No P&L data yet.</p>';
        return;
    }

    let html = `
        <table style="width: 100%; font-size: 12px; border-collapse: collapse;">
            <thead>
                <tr style="color: #888; text-align: left;">
                    <th style="padding: 4px;">Period</th>
                    <th style="padding: 4px;">Arb</th>
                    <th style="padding: 4px;">Fees</th>
                    <th style="padding: 4px;">Hedge</th>
                    <th style="padding: 4px;">Net</th>
                </tr>
            </thead>
            <tbody>
    `;

    for (const row of data.summary) {
        const netColor = row.net_profit >= 0 ? '#4ade80' : '#f87171';
        html += `
            <tr style="border-top: 1px solid #333;">
                <td style="padding: 4px;">${row.period}</td>
                <td style="padding: 4px; color: ${row.arb_profit >= 0 ? '#4ade80' : '#f87171'}">$${(row.arb_profit / 100).toFixed(2)}</td>
                <td style="padding: 4px; color: #f87171">-$${(row.fees / 100).toFixed(2)}</td>
                <td style="padding: 4px; color: ${row.hedge_pnl >= 0 ? '#4ade80' : '#f87171'}">$${row.hedge_pnl.toFixed(2)}</td>
                <td style="padding: 4px; color: ${netColor}">$${(row.net_profit / 100).toFixed(2)}</td>
            </tr>
        `;
    }

    html += '</tbody></table>';
    document.getElementById('pnl-summary-table').innerHTML = html;
}

// Load summary on page load
loadPnlSummary();
```

**Step 3: Commit**

```bash
git add static/index.html
git commit -m "feat(pnl): add P&L summary view with daily/weekly/monthly aggregation"
```

---

## Task 15: Add pnl.db to .gitignore

**Files:**
- Modify: `.gitignore` (create if doesn't exist)

**Step 1: Add database to gitignore**

```bash
echo "pnl.db" >> .gitignore
echo "pnl.db-wal" >> .gitignore
echo "pnl.db-shm" >> .gitignore
```

**Step 2: Commit**

```bash
git add .gitignore
git commit -m "chore: ignore pnl.db SQLite files"
```

---

## Summary

This implementation adds:

1. **`pnl_db.py`** - SQLite database module with:
   - Schema for fills, hedges, and matches
   - CRUD operations for all entities
   - P&L calculation with FIFO pairing
   - Period aggregation (daily/weekly/monthly)

2. **Dashboard integration**:
   - DB init on startup
   - Fills persisted from WebSocket
   - Matches persisted on add
   - New API endpoints: `/api/pnl/match/{id}`, `/api/pnl/summary`, `/api/hedges`

3. **Frontend**:
   - P&L display in match cards (arb profit, fees, net, leftover)
   - Hedge entry modal
   - P&L summary section with period selector

**Backfill** is intentionally not implemented - user will specify when to add it.
