# Kalshi Sports Market-Making Tool

## What This Does

Market-making on Kalshi's binary markets (esports, traditional sports). The strategy: quote both sides of a match below theo, capture spread when both sides fill.

```
Sportsbook odds (Pinnacle, The Odds API, etc.)
        ↓
Calculate no-vig theo probability
        ↓
Quote YES/NO on both teams at theo - edge
        ↓
If both fill: guaranteed profit (100c payout - total cost)
```

## Active Code

| File | Purpose |
|------|---------|
| `dashboard.py` | **Web dashboard** - FastAPI + WebSocket, multi-market MM with live monitoring, per-match settings |
| `mm.py` | CLI market maker - single match, adaptive quoting, inventory management |
| `valorant_mm.py` | Valorant scanner - finds markets, interactive theo entry |
| `market_scanner.py` | General scanner - NBA, NHL, NCAAB, esports, tennis, table tennis |
| `check_odds_coverage.py` | The Odds API coverage checker - NCAAB, UCL, FA Cup events |
| `config/config.py` | API keys (Kalshi, The Odds API, SportsGameOdds) |
| `static/index.html` | Dashboard frontend |
| `legacy/` | Archived code (NFL props, RRQ match) |

## Usage

### Dashboard (recommended for multiple markets)
```bash
python dashboard.py
# Open http://localhost:8000
```

Add matches via the web UI, enter tickers and decimal odds, then click Start.

Dashboard features:
- Real-time WebSocket updates (2s interval)
- Per-match settings: edge, contracts, inventory_max
- Order status with reasons (at ceiling, overexposed, competitor overbidding, rebalancing)
- Inventory sync from Kalshi positions
- Automatic order cancellation on shutdown or event start

### CLI - Single market
```bash
# Dry run
python mm.py --ticker-a KXCS2GAME-26FEB15R2BHE-R2 --ticker-b KXCS2GAME-26FEB15R2BHE-BHE \
    --odds-a 1.80 --odds-b 2.00 --dry-run

# Live trading
python mm.py --ticker-a KXCS2GAME-26FEB15R2BHE-R2 --ticker-b KXCS2GAME-26FEB15R2BHE-BHE \
    --odds-a 1.80 --odds-b 2.00 --contracts 10 --edge 1.0
```

### Scan for opportunities
```bash
# Valorant
python valorant_mm.py           # List markets with spreads
python valorant_mm.py --theo    # Interactive theo entry
python valorant_mm.py --odds    # Auto-fetch from OddsPapi

# All sports (NBA, NHL, NCAAB, esports, tennis, etc.)
python market_scanner.py                    # Scan active game-day markets
python market_scanner.py --mode all         # Full sports scan
python market_scanner.py --min-spread 5     # Filter by spread

# Check odds coverage
python check_odds_coverage.py   # See NCAAB, UCL, FA Cup events on The Odds API
```

## Core Concepts

### Theo Calculation
```python
# Decimal odds → no-vig probability
prob_a = 1 / odds_a
prob_b = 1 / odds_b
total = prob_a + prob_b  # >1 due to vig

theo_a = (prob_a / total) * 100  # cents
theo_b = (prob_b / total) * 100
```

### Adaptive Quoting
- Always stay at top of book (best bid)
- Never bid above `theo - edge_min` (the "ceiling")
- If outbid: raise by 1c (up to ceiling)
- If competitor goes above ceiling: back off, let them overpay
- Sticky ceiling: stay at top even if competition drops (retest periodically)

### Inventory Management
- Track net exposure: `+` = long Team A, `-` = long Team B
- At inventory limit: only quote reducing side
- Prevents runaway exposure if one side fills more than the other

### Rebalancing (dashboard)
- When at inventory limit, calculates breakeven ceiling from cost basis
- Raises bids on reducing side up to `100 - avg_cost - 1` to guarantee profit
- Tracks `cost_long_a`, `cost_long_b`, `count_long_a`, `count_long_b` per match

### Kalshi Price Mechanics
- Prices in cents (1-99)
- YES + NO always sums to 100
- Best YES ask = 100 - best NO bid
- Buying YES @ 45c = selling NO @ 55c (same exposure)

## Kalshi API

### Auth (RSA-PSS)
```python
# Sign: timestamp + method + path (no query params)
message = f"{timestamp_ms}GET/trade-api/v2/portfolio/balance"
signature = private_key.sign(message, PSS padding, SHA256)
```

Headers:
- `KALSHI-ACCESS-KEY`: API key ID
- `KALSHI-ACCESS-TIMESTAMP`: Unix ms
- `KALSHI-ACCESS-SIGNATURE`: Base64 signature

### Key Endpoints
| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /markets/{ticker}/orderbook` | No | Orderbook |
| `GET /markets?series_ticker=X` | No | List markets |
| `POST /portfolio/orders` | Yes | Place order |
| `DELETE /portfolio/orders/{id}` | Yes | Cancel order |
| `GET /portfolio/positions` | Yes | Current positions |
| `GET /portfolio/balance` | Yes | Account balance (cents) |

### Order Schema
```python
{
    "ticker": "KXCS2GAME-26FEB15-VIT",
    "action": "buy",
    "side": "yes",  # or "no"
    "type": "limit",
    "count": 10,
    "yes_price": 45,  # cents
    "expiration_ts": 1739610000  # Unix seconds, optional
}
```

## Series Tickers

### Esports
| Game | Series Ticker |
|------|---------------|
| Valorant | `KXVALORANTGAME` |
| CS2 | `KXCS2GAME` |
| LoL | `KXLOLMATCH`, `KXLOLCHAMP` |
| Dota 2 | `KXDOTA2` |
| COD | `KXCOD` |

### Traditional Sports (Binary only)
| Sport | Series Ticker |
|-------|---------------|
| NBA | `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL` |
| NHL | `KXNHLGAME`, `KXNHLTOTAL`, `KXNHLSPREAD` |
| NCAAB | `KXNCAAMBGAME`, `KXNCAAMBTOTAL` |
| Tennis | `KXATPMATCH`, `KXWTAMATCH` |
| Table Tennis | `KXTTELITEGAME` |
| Soccer (totals) | `KXEPLTOTAL`, `KXUCLTOTAL`, `KXLALIGATOTAL` |

Find markets:
```
GET /markets?series_ticker=KXCS2GAME&status=open
```

## Odds Sources

### The Odds API (primary)
- API key in `config/config.py`
- Covers: NCAAB, UCL, FA Cup, NBA, NHL, etc.
- Use `check_odds_coverage.py` to see available sports/events

### OddsPapi (esports)
- Free tier: 200 req/month
- Valorant sport ID: 61
- Good for: GGBet, Pinnacle, bet365

### Manual Entry
Use `valorant_mm.py --theo` to enter odds from:
- Pinnacle (sharpest)
- GGBet, Thunderpick, Rainbet (esports-focused)

## Dashboard API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serve frontend |
| `/api/state` | GET | Current state (matches, fills, balance) |
| `/api/matches` | POST | Add match (ticker_a, ticker_b, odds_a, odds_b, edge, contracts, inventory_max) |
| `/api/matches/{id}/start` | POST | Start trading |
| `/api/matches/{id}/stop` | POST | Stop trading, cancel orders |
| `/api/matches/{id}/settings` | POST | Update odds, edge, contracts, inventory_max, inventory |
| `/api/matches/{id}` | DELETE | Remove match |
| `/ws` | WebSocket | Real-time state updates |

## Legacy Files (in `legacy/` folder)

Archived code from previous versions:
- `rrq_prx_mm.py` - RRQ vs Paper Rex Valorant match MM (Feb 2026)
- `classification.py`, `theocalculator.py`, `getoddsfrombook.py`, `kalshiorderbook.py`, `main.py` - NFL player prop code (Jan 2026)
