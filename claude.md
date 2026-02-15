# Kalshi Market-Making Project - Context Reference

## Project Purpose

This project is an **NFL player prop market-making tool** for Kalshi's "Anytime Touchdown" prediction markets. The goal is to identify opportunities to profitably quote both sides (YES and NO) by using aggregated sportsbook odds as a fair value reference.

**Core Idea**: If sportsbooks collectively price a player's TD probability at 45%, and Kalshi's orderbook allows you to buy YES at 43% and NO at 53%, both sides are "cheap" — you can bid on both and potentially capture spread.

---

## High-Level Workflow

```
┌─────────────────────────────────────────────────────────────────┐
│  1. FETCH SPORTSBOOK ODDS                                       │
│     - Query SportsGameOdds API for player's anytime TD odds     │
│     - Get prices from FanDuel, DraftKings, BetMGM, etc.         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  2. CALCULATE THEO (Theoretical Fair Price)                     │
│     - Convert American odds → implied probability               │
│     - Apply weights by bookmaker sharpness                      │
│     - Remove ~5% vig to get "true" probability                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  3. FETCH KALSHI ORDERBOOK                                      │
│     - Query Kalshi API for player's TD market                   │
│     - Extract best YES bids/asks and NO bids/asks               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  4. CLASSIFY OPPORTUNITY                                        │
│     - Compare theo price to Kalshi's best prices                │
│     - If both sides priced above theo → "marketmakeable"        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  5. EXECUTE TRADES (NOT YET IMPLEMENTED)                        │
│     - Place limit orders on both sides                          │
│     - Manage positions, cancellations, amendments               │
└─────────────────────────────────────────────────────────────────┘
```

---

## Current Project Status

### What Works
- Fetching odds from SportsGameOdds API
- Computing weighted theo price with vig removal
- Fetching Kalshi orderbook (unauthenticated)

### What's Broken
- `classification.py:33` references undefined variables:
  ```python
  # These variables don't exist - code crashes here
  print(f"functional yes ask is {effective_yes_ask_per_contracts} ...")
  ```

### What's Missing
- Kalshi authentication (RSA-PSS signing)
- Order placement/cancellation
- Depth-weighted effective price calculation (`contracts` param unused)
- WebSocket for real-time data
- Position tracking
- Error handling

---

## Existing Code Patterns

### Bookmaker Weights (theocalculator.py)
```python
weights = {
    "betmgm": 0.4,
    "bovada": 0.3,
    "caesars": 0.5,
    "draftkings": 0.9,
    "espnbet": 0.3,
    "fanduel": 1.0   # Highest weight - considered sharpest
}
```

### American Odds → Probability (theocalculator.py)
```python
def american_to_prob(odd_str):
    odd = int(odd_str.strip())
    if odd > 0:
        return 100 / (odd + 100)
    else:
        return -odd / (-odd + 100)
```

### Vig Removal
```python
vig_factor = 0.05
no_vig_prob = weighted_implied / (1 + vig_factor)
```

### Kalshi Orderbook Parsing (kalshiorderbook.py)
```python
# Orderbook structure from Kalshi
# yes_bids/no_bids are lists of [price, quantity] pairs
yes_bids = sorted(orderbook_data['orderbook'].get('yes', []),
                  key=lambda x: x[0], reverse=True)[:5]
no_bids = sorted(orderbook_data['orderbook'].get('no', []),
                 key=lambda x: x[0], reverse=True)[:5]
```

### Price Conversion (classification.py)
```python
# Kalshi prices are in cents (1-99)
# Best YES bid at 45 means best NO ask is 55 (they sum to 100)
best_yes_bid = round(max([b[0] for b in yes_bids], default=0) / 100, 2)
best_no_bid = round(max([b[0] for b in no_bids], default=0) / 100, 2)

best_no_ask = round(1 - best_yes_bid, 2)
best_yes_ask = round(1 - best_no_bid, 2)
```

### Market Discovery (kalshiorderbook.py)
```python
series_ticker = "KXNFLANYTD"  # NFL Anytime TD series
markets_url = f"https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={series_ticker}&status=open"

# Filter by player name
player_markets = [
    market for market in td_markets
    if player_name.lower() in market.get("title", "").lower()
]
```

---

## Kalshi API Reference

### Base URLs
| Environment | REST API | WebSocket |
|-------------|----------|-----------|
| Production | `https://api.elections.kalshi.com/trade-api/v2` | `wss://api.elections.kalshi.com/trade-api/ws/v2` |
| Demo | `https://demo-api.kalshi.co/trade-api/v2` | `wss://demo-api.kalshi.co/trade-api/ws/v2` |

### Rate Limits

| Tier | Read/sec | Write/sec | Qualification |
|------|----------|-----------|---------------|
| Basic | 20 | 10 | Signup |
| Advanced | 30 | 30 | [Typeform](https://kalshi.typeform.com/advanced-api) |
| Premier | 100 | 100 | 3.75% monthly volume |
| Prime | 400 | 400 | 7.5% monthly volume |

**Write operations**: CreateOrder, CancelOrder, AmendOrder, DecreaseOrder, BatchCreateOrders, BatchCancelOrders

### Authentication

Kalshi uses RSA-PSS signed requests. Required headers:
- `KALSHI-ACCESS-KEY`: Your API key ID
- `KALSHI-ACCESS-TIMESTAMP`: Unix timestamp in milliseconds
- `KALSHI-ACCESS-SIGNATURE`: Base64-encoded RSA-PSS signature

**Signature format**: Sign the string `{timestamp}{METHOD}{path_without_query}`

```python
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
import base64
import datetime

def load_private_key(file_path):
    with open(file_path, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )

def sign_request(private_key, timestamp: str, method: str, path: str) -> str:
    message = f"{timestamp}{method}{path}".encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')

def get_auth_headers(key_id: str, private_key, method: str, path: str) -> dict:
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    path_clean = path.split('?')[0]
    signature = sign_request(private_key, timestamp, method, path_clean)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json"
    }
```

### Key Endpoints

#### Market Data (No Auth Required)
| Endpoint | Description |
|----------|-------------|
| `GET /markets` | List markets. Params: `series_ticker`, `status`, `limit`, `cursor` |
| `GET /markets/{ticker}` | Single market details |
| `GET /series/{series_ticker}` | Series template info |

#### Market Data (Auth Required)
| Endpoint | Description |
|----------|-------------|
| `GET /markets/{ticker}/orderbook` | Full orderbook. Param: `depth` (0-100, 0=all) |

#### Trading (Auth Required)
| Endpoint | Description |
|----------|-------------|
| `POST /portfolio/orders` | Place order |
| `GET /portfolio/orders` | List your orders |
| `DELETE /portfolio/orders/{order_id}` | Cancel order |
| `POST /portfolio/orders/{order_id}/amend` | Modify order |
| `POST /portfolio/orders/batched` | Batch create (max 20) |
| `DELETE /portfolio/orders/batched` | Batch cancel (max 20) |
| `GET /portfolio/balance` | Account balance (cents) |
| `GET /portfolio/positions` | Current positions |

#### Order Schema
```python
# Create order request
order = {
    "ticker": "KXNFLANYTD-26FEB09-JAMESCOOK",
    "action": "buy",      # "buy" or "sell"
    "side": "yes",        # "yes" or "no"
    "type": "limit",      # "limit" or "market"
    "count": 100,         # Number of contracts
    "yes_price": 45       # Price in cents (1-99)
    # OR "no_price": 55
}
```

### WebSocket Channels

| Channel | Auth | Description |
|---------|------|-------------|
| `orderbook_delta` | Yes | Real-time orderbook updates |
| `fill` | Yes | Your order fills |
| `ticker` | No | Price/volume updates |
| `trade` | No | Public trade feed |

**Subscription format**:
```python
{
    "id": 1,
    "cmd": "subscribe",
    "params": {
        "channels": ["orderbook_delta"],
        "market_tickers": ["KXNFLANYTD-26FEB09-JAMESCOOK"]
    }
}
```

---

## SportsGameOdds API (Currently Used)

### Base URL
`https://api.sportsgameodds.com/v2`

### Authentication
API key passed as query parameter: `apiKey={key}`

### Endpoints Used
```python
# Get events with odds for a specific player prop
GET /events?leagueID=NFL&oddsAvailable=true&oddID={odd_id}&limit=50&includeAltLines=true&apiKey={key}

# odd_id format for anytime TD
odd_id = f"touchdowns-{player_id}-game-yn-yes"
# Example: "touchdowns-JAMES_COOK_1_NFL-game-yn-yes"
```

### Response Structure
```python
{
    "data": [
        {
            "teams": {
                "home": {"teamID": "DENVER_BRONCOS_NFL"},
                "away": {"teamID": "BUFFALO_BILLS_NFL"}
            },
            "odds": {
                "touchdowns-PLAYER_ID-game-yn-yes": {
                    "byBookmaker": {
                        "fanduel": {"odds": "+120"},
                        "draftkings": {"odds": "+115"}
                    }
                }
            }
        }
    ]
}
```

---

## The Odds API (Alternative/Recommended)

Better player prop support than SportsGameOdds.

### Base URL
`https://api.the-odds-api.com/v4`

### Authentication
API key as query parameter: `apiKey={key}`

### Rate Limits
- Credits consumed per request (varies by endpoint)
- Headers show remaining quota: `x-requests-remaining`, `x-requests-used`

### Key Endpoints
| Endpoint | Cost | Description |
|----------|------|-------------|
| `GET /sports/` | Free | List sports |
| `GET /sports/{sport}/events/` | Free | List events |
| `GET /sports/{sport}/events/{eventId}/odds/` | 1 credit/region/market | Event odds |

### Player Prop Markets (NFL)
| Market Key | Description |
|------------|-------------|
| `player_anytime_td` | Anytime touchdown scorer (Yes/No) |
| `player_1st_td` | First touchdown scorer |
| `player_pass_tds` | Passing TDs (O/U) |
| `player_rush_tds` | Rushing TDs (O/U) |

### Example Request
```python
GET /sports/americanfootball_nfl/events/{event_id}/odds
    ?apiKey={key}
    &regions=us
    &markets=player_anytime_td
    &oddsFormat=american
```

---

## Migration Notes: New Markets

### Changing Series/Market Type

To target a different market on Kalshi:

1. **Find the series ticker**:
   ```python
   GET /series?category=sports
   # Look for series_ticker like "KXNFLANYTD", "KXNFLPTS", etc.
   ```

2. **Update market discovery**:
   ```python
   series_ticker = "KXNEW_SERIES"  # New series ticker
   markets_url = f".../markets?series_ticker={series_ticker}&status=open"
   ```

3. **Adjust filtering logic** - title format may differ per series

### Schema Differences

- **Binary markets**: YES/NO, prices sum to 100
- **Multivariate markets**: Multiple outcomes, use `/events/multivariate` endpoint
- **Ranged markets**: May have different settlement rules

### Trading Rules to Check

- Fee structure (currently 0 maker fees on most markets)
- Settlement timing
- Position limits
- Market hours (some markets close during events)

### Odds Source Considerations

Different prop types may need different odds sources:
- Anytime TD: `player_anytime_td` on The Odds API
- Points markets: `player_points`
- Passing yards: `player_pass_yds`

Map Kalshi market types to appropriate odds API market keys.

---

## File Structure

```
SSMMproppicks/
├── config/
│   ├── config.py           # API keys
│   ├── apiusesleft.py      # Check SportsGameOdds quota
│   └── fetchplayers.py     # List players for a game
├── getoddsfrombook.py      # Fetch sportsbook odds
├── theocalculator.py       # Calculate theo price
├── kalshiorderbook.py      # Fetch Kalshi orderbook
├── classification.py       # Compare theo vs market (BROKEN)
├── main.py                 # Entry point example
└── claude.md               # This file
```

---

## Next Steps to Complete

1. **Fix classification.py** - implement `effective_yes_ask_per_contracts` calculation
2. **Add Kalshi authentication** - implement RSA-PSS signing
3. **Build order execution** - place/cancel/amend orders
4. **Add WebSocket** - real-time orderbook streaming
5. **Create trading loop** - continuous monitoring and quoting
6. **Add position management** - track exposure, P&L
7. **Error handling** - retries, rate limit handling, logging

---

## Useful Links

- [Kalshi API Docs](https://docs.kalshi.com/welcome)
- [Kalshi Rate Limits](https://docs.kalshi.com/getting_started/rate_limits)
- [Kalshi API Keys](https://docs.kalshi.com/getting_started/api_keys)
- [Kalshi WebSocket Guide](https://docs.kalshi.com/getting_started/quick_start_websockets)
- [Kalshi Python SDK](https://pypi.org/project/kalshi-python/)
- [The Odds API Docs](https://the-odds-api.com/liveapi/guides/v4/)
- [The Odds API Markets](https://the-odds-api.com/sports-odds-data/betting-markets.html)
