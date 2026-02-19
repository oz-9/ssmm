# PnL Tracking System Design

## Overview

Track profit and loss for the Kalshi market-making dashboard with:
- Realized P&L from completed arbs
- Theoretical EV vs actual value for leftover inventory
- Fee tracking
- Manual hedge entry (linked to matches, for external platforms like Thunderpick)
- Per-match, daily, weekly, monthly views
- Historical backfill from Kalshi API (deferred - user will specify when)

## Database Schema (SQLite)

### `fills` table
Raw fill data from Kalshi.

```sql
CREATE TABLE fills (
    id TEXT PRIMARY KEY,           -- Kalshi fill_id
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,            -- 'yes' or 'no'
    action TEXT NOT NULL,          -- 'buy' or 'sell'
    price INTEGER NOT NULL,        -- cents
    count INTEGER NOT NULL,
    is_taker BOOLEAN,
    fee_cost INTEGER,              -- cents
    created_time TEXT NOT NULL,    -- ISO timestamp
    match_id TEXT,                 -- links to dashboard match (nullable for backfill)
    synced_at TEXT NOT NULL        -- when we fetched this
);
CREATE INDEX idx_fills_ticker ON fills(ticker);
CREATE INDEX idx_fills_match_id ON fills(match_id);
CREATE INDEX idx_fills_created_time ON fills(created_time);
```

### `hedges` table
Manual hedge entries for external platforms.

```sql
CREATE TABLE hedges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,        -- links to dashboard match
    platform TEXT NOT NULL,        -- 'thunderpick', 'ggbet', etc.
    side TEXT NOT NULL,            -- 'team_a' or 'team_b'
    amount_usd REAL NOT NULL,      -- stake amount
    odds REAL NOT NULL,            -- decimal odds
    outcome TEXT,                  -- 'win', 'loss', 'push', null=pending
    created_at TEXT NOT NULL,
    FOREIGN KEY (match_id) REFERENCES matches(id)
);
CREATE INDEX idx_hedges_match_id ON hedges(match_id);
```

### `matches` table
Match metadata for linking fills and hedges.

```sql
CREATE TABLE matches (
    id TEXT PRIMARY KEY,           -- dashboard match ID
    ticker_a TEXT NOT NULL,
    ticker_b TEXT NOT NULL,
    theo_a INTEGER,                -- stored theo at match start (cents)
    theo_b INTEGER,
    event_time TEXT,
    settled_at TEXT,               -- when match resolved (null if pending)
    category TEXT
);
CREATE INDEX idx_matches_ticker_a ON matches(ticker_a);
CREATE INDEX idx_matches_ticker_b ON matches(ticker_b);
```

## P&L Calculations

### Per-Match Breakdown

1. **Pure Arb Profit**: When both sides filled on same match
   - Revenue: 100c per completed pair
   - Cost: price_a + price_b for each pair
   - Profit: `(100 - price_a - price_b) * paired_contracts`
   - Pairs are matched FIFO by fill time

2. **Leftover Inventory**: Unpaired contracts after pairing
   - **Theoretical EV**: `count * (theo / 100) * 100c`
   - **Actual Value**:
     - Settled: actual payout (100c if won, 0c if lost)
     - Pending: current market mid price * count
   - **EV vs AV**: `actual_value - theoretical_ev`

3. **Fees**: Sum of `fee_cost` from all fills for the match

4. **Hedges**: Net profit/loss from external platform bets
   - Win: `amount_usd * (odds - 1)`
   - Loss: `-amount_usd`
   - Push: `0`

5. **Net Profit**: `arb_profit + leftover_av - fees + hedge_pnl`

### Aggregation Views

Group by date/week/month and sum:
- Total arb profit
- Total leftover EV
- Total leftover AV
- Total hedge P&L
- Total fees
- Net profit

## Implementation Components

### 1. Database Module (`pnl_db.py`)

- SQLite connection with WAL mode for concurrent access
- Schema initialization on first run
- CRUD operations:
  - `insert_fill(fill_data)` - upsert fill from Kalshi
  - `insert_hedge(match_id, platform, side, amount, odds)`
  - `update_hedge_outcome(hedge_id, outcome)`
  - `insert_match(match_data)` - store match metadata
  - `get_match_pnl(match_id)` - full P&L breakdown
  - `get_summary(period)` - aggregated P&L

### 2. Sync Service

- On dashboard start: fetch recent fills from Kalshi API
- Link fills to matches by ticker (fills with ticker_a or ticker_b belong to match)
- Backfill: separate function to import historical fills (user-triggered)

### 3. Dashboard API Endpoints

```
GET  /api/pnl/match/{id}              Per-match P&L breakdown
GET  /api/pnl/summary?period=daily    Aggregated view (daily|weekly|monthly)
POST /api/hedges                      Add hedge entry
PUT  /api/hedges/{id}                 Update hedge outcome
GET  /api/hedges?match_id={id}        List hedges for match
```

### 4. Frontend Updates

- P&L summary section (collapsible, like categories)
- Per-match P&L display in match cards (small text below status)
- Hedge entry button per match -> modal form
- P&L detail view (click to expand)

## Backfill Process (Deferred)

When triggered:
1. Call `GET /portfolio/fills` with pagination
2. Filter by date range
3. Insert into `fills` table (skip duplicates by fill_id)
4. Match to existing dashboard matches by ticker
5. Create placeholder match records for unlinked fills

User will specify when to implement backfill.

## File Structure

```
pnl_db.py          Database module
dashboard.py       Add API endpoints, integrate sync
static/index.html  Frontend P&L components
```

## Database Location

`pnl.db` in project root, gitignored.
