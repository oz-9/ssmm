"""
Multi-market dashboard for Kalshi market making.
Run: python dashboard.py
Open: http://localhost:8000
"""

import asyncio
import atexit
import concurrent.futures
import datetime
import json
import math
import signal
import sys
from dataclasses import dataclass, asdict
from typing import Optional
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import pnl_db

# Import from mm.py
from mm import (
    KalshiClient,
    KalshiWebSocket,
    get_book_with_depth,
    calculate_adaptive_price,
    calculate_theo,
    get_event_start_time,
    OrderState,
    KALSHI_BASE_URL,
)
from lacrosse_scanner import scan as scan_lacrosse, get_odds_api_events as get_lacrosse_odds
from boxing_scanner import scan as scan_boxing, get_odds_api_events as get_boxing_odds

# =============================================================================
# RATE TRACKING & SETTINGS
# =============================================================================

import time

REBAL_FEE_BUFFER_CENTS = 2  # Maker fees: ~1c entry + ~1c rebalance (ceil(0.0175 * P * (1-P)) per leg)

# Global timing settings
class Settings:
    check_interval: float = 2.0      # How often to check/update quotes (seconds)
    sticky_reset_secs: float = 10.0  # How long to stay at ceiling before retesting
    overbid_cancel_delay: float = 10.0  # How long to wait before cancelling when overbid

settings = Settings()

# Flag to prevent multiple shutdown attempts
_shutdown_done = False

def emergency_cancel_all_orders():
    """Synchronous emergency cancel - called on exit/signal."""
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True

    print("\n[EMERGENCY] Cancelling all orders...")
    try:
        from config.config import KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH
        emergency_client = KalshiClient(KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH)

        # Fetch all resting orders
        resp = emergency_client.get('/portfolio/orders?status=resting')
        resting = resp.json().get('orders', [])

        if not resting:
            print("[EMERGENCY] No resting orders found")
            return

        print(f"[EMERGENCY] Found {len(resting)} resting orders, cancelling...")

        # Cancel in parallel
        def cancel_one(order_id):
            try:
                emergency_client.cancel_order(order_id)
                return True
            except:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            order_ids = [o['order_id'] for o in resting]
            results = list(executor.map(cancel_one, order_ids))
            print(f"[EMERGENCY] Cancelled {sum(results)}/{len(order_ids)} orders")

    except Exception as e:
        print(f"[EMERGENCY] Failed to cancel orders: {e}")

# Register atexit handler
atexit.register(emergency_cancel_all_orders)

# Register signal handlers (for Ctrl+C)
def signal_handler(signum, frame):
    print(f"\n[SIGNAL] Received signal {signum}")
    emergency_cancel_all_orders()
    sys.exit(0)

# Register SIGINT on all platforms (Ctrl+C)
signal.signal(signal.SIGINT, signal_handler)
# SIGTERM only on Unix
if sys.platform != 'win32':
    signal.signal(signal.SIGTERM, signal_handler)

# =============================================================================
# CATEGORY DETECTION
# =============================================================================

TICKER_CATEGORIES = {
    "KXNCAAMLAXGAME": "Men's College Lacrosse",
    "KXBOXING": "Boxing",
    "KXNBAGAME": "NBA",
    "KXNHLGAME": "NHL",
    "KXNCAAMBGAME": "NCAAB",
    "KXVALORANTGAME": "Valorant",
    "KXCS2GAME": "CS2",
    "KXLOLMATCH": "League of Legends",
    "KXDOTA2": "Dota 2",
    "KXCOD": "Call of Duty",
    "KXATPMATCH": "ATP Tennis",
    "KXWTAMATCH": "WTA Tennis",
    "KXTTELITEGAME": "Table Tennis",
}

def get_category(ticker: str) -> str:
    """Get category name from ticker prefix."""
    for prefix, category in TICKER_CATEGORIES.items():
        if ticker.startswith(prefix):
            return category
    return "Other"

# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class Market:
    """A single market (one side of a match)."""
    ticker: str
    label: str
    theo: float  # cents
    best_bid: int = 0
    best_ask: int = 0
    second_bid: int = 0
    second_ask: int = 0
    our_bid: Optional[int] = None
    our_ask: Optional[int] = None

@dataclass
class OrderStatus:
    """Status of an order slot."""
    price: Optional[int] = None  # None if no order
    reason: Optional[str] = None  # Why no order (if price is None)
    order_id: Optional[str] = None

@dataclass
class Match:
    """A match with two markets (Team A vs Team B)."""
    id: str
    name: str
    category: str  # e.g., "Men's College Lacrosse", "Boxing"
    market_a: Market
    market_b: Market
    odds_a: float  # decimal odds
    odds_b: float
    inventory: int = 0  # + = long A, - = long B
    active: bool = False
    event_time: Optional[datetime.datetime] = None
    # Per-match settings
    edge: float = 2.5
    contracts: int = 5
    inventory_max: int = 15
    # Order statuses for all 4 positions
    a_yes_status: OrderStatus = None
    a_no_status: OrderStatus = None
    b_yes_status: OrderStatus = None
    b_no_status: OrderStatus = None
    # Cost tracking for rebalancing
    cost_long_a: int = 0  # total cents spent going long A
    count_long_a: int = 0  # contracts long A
    cost_long_b: int = 0  # total cents spent going long B
    count_long_b: int = 0  # contracts long B
    # URL for linking to Kalshi
    market_url: Optional[str] = None
    # Last fill tracking (when someone traded with us)
    last_fill_time: Optional[str] = None
    last_fill_desc: Optional[str] = None

    def __post_init__(self):
        if self.a_yes_status is None:
            self.a_yes_status = OrderStatus()
        if self.a_no_status is None:
            self.a_no_status = OrderStatus()
        if self.b_yes_status is None:
            self.b_yes_status = OrderStatus()
        if self.b_no_status is None:
            self.b_no_status = OrderStatus()

@dataclass
class Fill:
    """A fill event."""
    timestamp: str
    match_id: str
    side: str  # "A YES", "B NO", etc.
    price: int
    count: int
    complete_set: bool = False  # True if this completes an arb
    profit: Optional[int] = None  # profit in cents if complete

class MatchConfig(BaseModel):
    """Config for adding a new match."""
    ticker_a: str
    ticker_b: str
    odds_a: float
    odds_b: float
    name: Optional[str] = None
    edge: float = 2.5
    contracts: int = 5
    inventory_max: int = 15
    event_time: Optional[str] = None  # ISO format, from Odds API commence_time

class UpdateOdds(BaseModel):
    """Update odds for a match."""
    odds_a: float
    odds_b: float

class UpdateSettings(BaseModel):
    """Update settings for a match."""
    odds_a: Optional[float] = None
    odds_b: Optional[float] = None
    edge: Optional[float] = None
    contracts: Optional[int] = None
    inventory_max: Optional[int] = None
    inventory: Optional[int] = None

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

# =============================================================================
# GLOBAL STATE
# =============================================================================

matches: dict[str, Match] = {}
fills: list[Fill] = []
orders: dict[str, OrderState] = {}  # (match_id, ticker, side) -> OrderState
order_locks: dict[str, asyncio.Lock] = {}  # Per-order-key locks for race condition prevention
order_locks_lock: Optional[asyncio.Lock] = None  # Lock for creating new order locks
overbid_since: dict[str, float] = {}  # order_key -> timestamp when overbid first detected
client: Optional[KalshiClient] = None
ws_client: Optional["KalshiWebSocket"] = None
websockets: list[WebSocket] = []
trading_task: Optional[asyncio.Task] = None

# Config (now using settings object, these are kept for reference)
# CHECK_INTERVAL = settings.check_interval
# OVERBID_CANCEL_DELAY = settings.overbid_cancel_delay

# =============================================================================
# KALSHI CLIENT
# =============================================================================

def init_client():
    """Initialize Kalshi REST and WebSocket clients from config."""
    global client, ws_client
    try:
        from config.config import KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH
        client = KalshiClient(KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH)
        ws_client = KalshiWebSocket(KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH)
        balance = client.get_balance()
        print(f"Kalshi authenticated. Balance: ${balance.get('balance', 0) / 100:.2f}")
        return True
    except Exception as e:
        print(f"Kalshi auth failed: {e}")
        return False

def get_balance() -> float:
    """Get account balance in dollars."""
    if not client:
        return 0.0
    try:
        return client.get_balance().get('balance', 0) / 100
    except:
        return 0.0

def get_positions(ticker: str) -> tuple[int, int]:
    """Get current YES and NO positions for a ticker."""
    if not client:
        return 0, 0
    try:
        resp = client.get(f"/portfolio/positions?ticker={ticker}")
        positions = resp.json().get("market_positions", [])
        for pos in positions:
            if pos.get("ticker") == ticker:
                # Kalshi returns separate yes/no counts
                yes_pos = pos.get("position", 0)  # positive = long YES
                # If position is negative, that means short YES which isn't typical
                # Usually you have resting_orders_count for pending
                return max(yes_pos, 0), max(-yes_pos, 0) if yes_pos < 0 else 0
        return 0, 0
    except Exception as e:
        print(f"Error fetching position for {ticker}: {e}")
        return 0, 0

def calculate_match_inventory(match: "Match") -> int:
    """Calculate inventory from Kalshi positions. + = long A, - = long B.

    A YES = long A
    A NO = long B (same as B YES)
    B YES = long B
    B NO = long A (same as A YES)
    """
    a_yes, a_no = get_positions(match.market_a.ticker)
    b_yes, b_no = get_positions(match.market_b.ticker)

    # Long A exposure = A YES + B NO
    # Long B exposure = A NO + B YES
    long_a = a_yes + b_no
    long_b = a_no + b_yes

    return long_a - long_b

# =============================================================================
# MARKET LOGIC
# =============================================================================

def get_label(ticker: str) -> str:
    """Extract short label from ticker."""
    return ticker.split("-")[-1] if "-" in ticker else ticker[:6]

def get_kalshi_market_url(ticker: str) -> Optional[str]:
    """Get the Kalshi web URL for a market ticker."""
    if not client:
        return None
    try:
        # Get market info to find event_ticker
        market = client.get_market(ticker)
        event_ticker = market.get("event_ticker", "")

        # Extract series_ticker from event_ticker (first part before the date suffix)
        # e.g., KXNCAAMLAXGAME-26FEB17ROBSTB -> KXNCAAMLAXGAME
        parts = event_ticker.split("-")
        series_ticker = parts[0] if parts else ""

        if not series_ticker or not event_ticker:
            return None

        # Get series info to find the slug
        resp = client.get(f"/series/{series_ticker}")
        series = resp.json().get("series", {})

        # The slug comes from the series title, converted to lowercase with dashes
        # e.g., "Men's College Lacrosse Game" -> "mens-college-lacrosse-game"
        # But Kalshi provides a "ticker_name" or we can use category/subtitle
        series_title = series.get("title", "")

        # Generate slug from title: lowercase, replace spaces with dashes, remove special chars
        import re
        slug = series_title.lower()
        slug = re.sub(r"['\"]", "", slug)  # Remove quotes/apostrophes
        slug = re.sub(r"[^a-z0-9\s-]", "", slug)  # Remove other special chars
        slug = re.sub(r"\s+", "-", slug)  # Replace spaces with dashes
        slug = re.sub(r"-+", "-", slug)  # Collapse multiple dashes
        slug = slug.strip("-")

        if not slug:
            slug = series_ticker.lower()

        # Construct URL: /markets/{series}/{slug}/{event}
        url = f"https://kalshi.com/markets/{series_ticker.lower()}/{slug}/{event_ticker.lower()}"
        return url
    except Exception as e:
        print(f"Error getting market URL for {ticker}: {e}")
        return None

def add_match(config: MatchConfig) -> Match:
    """Add a new match to track."""
    theo = calculate_theo(config.odds_a, config.odds_b)

    label_a = get_label(config.ticker_a)
    label_b = get_label(config.ticker_b)

    match_id = f"{label_a}v{label_b}"

    # Get full event name and market URL from Kalshi
    name = config.name
    market_url = None
    if client:
        try:
            market = client.get_market(config.ticker_a)
            # Market title is like "Robert Morris vs Stony Brook"
            if not name:
                name = market.get("title") or market.get("subtitle") or f"{label_a} vs {label_b}"
            # Get the URL for linking
            market_url = get_kalshi_market_url(config.ticker_a)
        except:
            pass

    if not name:
        name = f"{label_a} vs {label_b}"

    # Use provided event_time (from Odds API) if available, else fall back to Kalshi's time
    if config.event_time:
        event_time = datetime.datetime.fromisoformat(config.event_time.replace("Z", "+00:00"))
    else:
        event_time = get_event_start_time(config.ticker_a)

    match = Match(
        id=match_id,
        name=name,
        category=get_category(config.ticker_a),
        market_a=Market(ticker=config.ticker_a, label=label_a, theo=theo["a"]),
        market_b=Market(ticker=config.ticker_b, label=label_b, theo=theo["b"]),
        odds_a=config.odds_a,
        odds_b=config.odds_b,
        event_time=event_time,
        edge=config.edge,
        contracts=config.contracts,
        inventory_max=config.inventory_max,
        market_url=market_url,
    )

    # Fetch current positions from Kalshi
    match.inventory = calculate_match_inventory(match)

    matches[match_id] = match

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

    return match

def update_match_odds(match_id: str, odds_a: float, odds_b: float):
    """Update odds for a match."""
    if match_id not in matches:
        return

    match = matches[match_id]
    match.odds_a = odds_a
    match.odds_b = odds_b

    theo = calculate_theo(odds_a, odds_b)
    match.market_a.theo = theo["a"]
    match.market_b.theo = theo["b"]

def refresh_match_odds(match_id: str) -> dict:
    """Refresh odds for a match from Odds API. Returns new odds or error."""
    if match_id not in matches:
        return {"error": "Match not found"}

    match = matches[match_id]
    ticker_a = match.market_a.ticker

    # Detect sport from ticker prefix
    if ticker_a.startswith("KXNCAAMLAXGAME"):
        # Lacrosse
        try:
            odds_events = get_lacrosse_odds()
            # Find matching event by checking if match name appears in odds data
            match_name_lower = match.name.lower()

            for event in odds_events:
                home = event.get("home_team", "")
                away = event.get("away_team", "")
                home_lower = home.lower()
                away_lower = away.lower()

                # Check if teams match
                if any(t in match_name_lower for t in [home_lower.split()[0], away_lower.split()[0]]):
                    # Pinnacle 60%, average of others 40%
                    pinnacle_home = pinnacle_away = None
                    other_home = []
                    other_away = []

                    for bm in event.get("bookmakers", []):
                        bm_key = bm["key"]
                        home_price = away_price = None
                        for outcome in bm["markets"][0]["outcomes"]:
                            if outcome["name"] == home:
                                home_price = outcome["price"]
                            if outcome["name"] == away:
                                away_price = outcome["price"]

                        if home_price and away_price:
                            if bm_key == "pinnacle":
                                pinnacle_home = home_price
                                pinnacle_away = away_price
                            else:
                                other_home.append(home_price)
                                other_away.append(away_price)

                    # Calculate: 60% Pinnacle, 40% average of others
                    if pinnacle_home and other_home:
                        best_home = 0.6 * pinnacle_home + 0.4 * (sum(other_home) / len(other_home))
                        best_away = 0.6 * pinnacle_away + 0.4 * (sum(other_away) / len(other_away))
                    elif pinnacle_home:
                        best_home, best_away = pinnacle_home, pinnacle_away
                    elif other_home:
                        best_home = sum(other_home) / len(other_home)
                        best_away = sum(other_away) / len(other_away)
                    else:
                        continue

                    # Determine which is odds_a and which is odds_b based on ticker labels
                    label_a = match.market_a.label.lower()
                    if label_a in home_lower:
                        odds_a, odds_b = best_home, best_away
                    else:
                        odds_a, odds_b = best_away, best_home

                    # Calculate no-vig fair odds
                    imp_a = 1 / odds_a
                    imp_b = 1 / odds_b
                    total = imp_a + imp_b
                    fair_a = total / imp_a
                    fair_b = total / imp_b

                    update_match_odds(match_id, round(fair_a, 2), round(fair_b, 2))
                    return {"odds_a": round(fair_a, 2), "odds_b": round(fair_b, 2)}

            return {"error": "No matching odds found"}
        except Exception as e:
            return {"error": str(e)}

    elif ticker_a.startswith("KXBOXING"):
        # Boxing
        try:
            odds_events = get_boxing_odds()
            match_name_lower = match.name.lower()

            for event in odds_events:
                home = event.get("home_team", "")
                away = event.get("away_team", "")
                home_lower = home.lower()
                away_lower = away.lower()

                # Check if fighters match
                home_parts = home_lower.split()
                away_parts = away_lower.split()
                if any(p in match_name_lower for p in home_parts) and any(p in match_name_lower for p in away_parts):
                    # Pinnacle 60%, average of others 40%
                    pinnacle_home = pinnacle_away = pinnacle_draw = None
                    other_home = []
                    other_away = []
                    other_draw = []

                    for bm in event.get("bookmakers", []):
                        bm_key = bm["key"]
                        home_price = away_price = draw_price = None
                        for outcome in bm["markets"][0]["outcomes"]:
                            if outcome["name"] == home:
                                home_price = outcome["price"]
                            elif outcome["name"] == away:
                                away_price = outcome["price"]
                            elif outcome["name"].lower() == "draw":
                                draw_price = outcome["price"]

                        if home_price and away_price:
                            if bm_key == "pinnacle":
                                pinnacle_home = home_price
                                pinnacle_away = away_price
                                pinnacle_draw = draw_price
                            else:
                                other_home.append(home_price)
                                other_away.append(away_price)
                                if draw_price:
                                    other_draw.append(draw_price)

                    # Calculate: 60% Pinnacle, 40% average of others
                    if pinnacle_home and other_home:
                        best_home = 0.6 * pinnacle_home + 0.4 * (sum(other_home) / len(other_home))
                        best_away = 0.6 * pinnacle_away + 0.4 * (sum(other_away) / len(other_away))
                        if pinnacle_draw and other_draw:
                            best_draw = 0.6 * pinnacle_draw + 0.4 * (sum(other_draw) / len(other_draw))
                        elif pinnacle_draw:
                            best_draw = pinnacle_draw
                        elif other_draw:
                            best_draw = sum(other_draw) / len(other_draw)
                        else:
                            best_draw = 0
                    elif pinnacle_home:
                        best_home, best_away = pinnacle_home, pinnacle_away
                        best_draw = pinnacle_draw if pinnacle_draw else 0
                    elif other_home:
                        best_home = sum(other_home) / len(other_home)
                        best_away = sum(other_away) / len(other_away)
                        best_draw = sum(other_draw) / len(other_draw) if other_draw else 0
                    else:
                        continue

                    # Calculate with draw adjustment (draw splits 50/50)
                    imp_a = 1 / best_away
                    imp_b = 1 / best_home
                    imp_draw = 1 / best_draw if best_draw > 0 else 1/20
                    total = imp_a + imp_b + imp_draw

                    novig_a = imp_a / total + (imp_draw / total) / 2
                    novig_b = imp_b / total + (imp_draw / total) / 2

                    fair_a = 1 / novig_a
                    fair_b = 1 / novig_b

                    update_match_odds(match_id, round(fair_a, 2), round(fair_b, 2))
                    return {"odds_a": round(fair_a, 2), "odds_b": round(fair_b, 2)}

            return {"error": "No matching odds found"}
        except Exception as e:
            return {"error": str(e)}

    else:
        return {"error": f"Unsupported sport for ticker {ticker_a}"}

async def on_orderbook_change(ticker: str, book: dict):
    """Handle orderbook update from WebSocket."""
    # Find which match this ticker belongs to
    for match in matches.values():
        if not match.active:
            continue

        if ticker == match.market_a.ticker:
            match.market_a.best_bid = book["best_bid"]
            match.market_a.best_ask = book["best_ask"]
            match.market_a.second_bid = book.get("second_bid", 0)
            match.market_a.second_ask = book.get("second_ask", 0)
            await handle_match_update(match)
            break
        elif ticker == match.market_b.ticker:
            match.market_b.best_bid = book["best_bid"]
            match.market_b.best_ask = book["best_ask"]
            match.market_b.second_bid = book.get("second_bid", 0)
            match.market_b.second_ask = book.get("second_ask", 0)
            await handle_match_update(match)
            break

async def handle_match_update(match: Match):
    """Process a match when its orderbook changes."""
    now = datetime.datetime.now(datetime.timezone.utc)

    # Check event time
    if match.event_time and now >= match.event_time:
        print(f"[{match.id}] Event started - stopping")
        match.active = False
        await cancel_match_orders(match)
        return

    # Get books - try WebSocket cache first, fall back to REST (in parallel)
    async def get_book_async(ticker: str) -> dict:
        if ws_client:
            book = ws_client.get_book(ticker)
            # Check if cache has data (best_bid > 0 means we have real data)
            if book and book.get("best_bid", 0) > 0:
                return book
        # Fall back to REST (in thread to not block)
        return await asyncio.to_thread(get_book_with_depth, ticker)

    book_a, book_b = await asyncio.gather(
        get_book_async(match.market_a.ticker),
        get_book_async(match.market_b.ticker),
    )

    # Update quotes
    await update_quotes(match, book_a, book_b)

    # Broadcast state to dashboard clients
    await broadcast(get_state())

async def on_fill(fill_data: dict):
    """Handle fill notification from WebSocket."""
    global fills, orders

    ticker = fill_data.get("market_ticker")
    order_id = fill_data.get("order_id")
    side = fill_data.get("side")  # "yes" or "no"
    price = fill_data.get("yes_price") if side == "yes" else fill_data.get("no_price", fill_data.get("yes_price"))
    count = fill_data.get("count", 0)
    post_position = fill_data.get("post_position", 0)

    # Find which match this fill belongs to
    for match in matches.values():
        if ticker not in [match.market_a.ticker, match.market_b.ticker]:
            continue

        # Track cost basis
        fill_cost = count * price
        if (ticker == match.market_a.ticker and side == "yes") or \
           (ticker == match.market_b.ticker and side == "no"):
            # Going long A
            match.cost_long_a += fill_cost
            match.count_long_a += count
        else:
            # Going long B
            match.cost_long_b += fill_cost
            match.count_long_b += count

        # Record fill for display
        label = match.market_a.label if ticker == match.market_a.ticker else match.market_b.label
        fill_time = datetime.datetime.now().strftime("%H:%M:%S")
        fill = Fill(
            timestamp=fill_time,
            match_id=match.id,
            side=f"{label} {side.upper()}",
            price=price,
            count=count,
        )
        fills.append(fill)
        print(f"[{match.id}] FILL (WS): {fill.side} {count}@{price}c")

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

        # Track last fill for this match
        match.last_fill_time = fill_time
        match.last_fill_desc = f"{label} {side.upper()} {count}@{price}c"

        # Remove filled order from tracking
        order_key = f"{match.id}:{ticker}:{side}"
        if order_key in orders:
            del orders[order_key]

        # Broadcast updated state
        await broadcast(get_state())
        break

async def on_position_change(position_data: dict):
    """Handle position update from WebSocket."""
    ticker = position_data.get("market_ticker")

    # Find which match this position belongs to and recalculate inventory
    for match in matches.values():
        if ticker == match.market_a.ticker or ticker == match.market_b.ticker:
            # Recalculate inventory from Kalshi positions
            old_inv = match.inventory
            match.inventory = await asyncio.to_thread(calculate_match_inventory, match)
            if match.inventory != old_inv:
                print(f"[{match.id}] Inventory updated: {old_inv} -> {match.inventory}")
            await broadcast(get_state())
            break

# =============================================================================
# TRADING LOOP
# =============================================================================

async def broadcast(data: dict):
    """Send data to all connected websockets."""
    message = json.dumps(data, default=str)
    for ws in websockets[:]:
        try:
            await ws.send_text(message)
        except:
            if ws in websockets:
                websockets.remove(ws)

def get_state() -> dict:
    """Get current state for dashboard."""
    return {
        "type": "state",
        "balance": get_balance(),
        "matches": [
            {
                "id": m.id,
                "name": m.name,
                "category": get_category(m.market_a.ticker),  # Derive from ticker
                "active": m.active,
                "inventory": m.inventory,
                "market_url": m.market_url,
                "last_fill_time": m.last_fill_time,
                "last_fill_desc": m.last_fill_desc,
                "market_a": {
                    "label": m.market_a.label,
                    "ticker": m.market_a.ticker,
                    "theo": m.market_a.theo,
                    "best_bid": m.market_a.best_bid,
                    "best_ask": m.market_a.best_ask,
                    "second_bid": m.market_a.second_bid,
                    "second_ask": m.market_a.second_ask,
                },
                "market_b": {
                    "label": m.market_b.label,
                    "ticker": m.market_b.ticker,
                    "theo": m.market_b.theo,
                    "best_bid": m.market_b.best_bid,
                    "best_ask": m.market_b.best_ask,
                    "second_bid": m.market_b.second_bid,
                    "second_ask": m.market_b.second_ask,
                },
                "orders": {
                    "a_yes": {"price": m.a_yes_status.price, "reason": m.a_yes_status.reason},
                    "a_no": {"price": m.a_no_status.price, "reason": m.a_no_status.reason},
                    "b_yes": {"price": m.b_yes_status.price, "reason": m.b_yes_status.reason},
                    "b_no": {"price": m.b_no_status.price, "reason": m.b_no_status.reason},
                },
                "odds_a": m.odds_a,
                "odds_b": m.odds_b,
                "edge": m.edge,
                "contracts": m.contracts,
                "inventory_max": m.inventory_max,
                "event_time": m.event_time.isoformat() if m.event_time else None,
                "pnl": pnl_db.calculate_match_pnl(
                    m.id,
                    int(m.market_a.theo),
                    int(m.market_b.theo)
                ) if pnl_db.get_match(m.id) else None,
            }
            for m in matches.values()
        ],
        "fills": [asdict(f) for f in fills[-10:]],  # Last 10 fills
        "settings": {
            "check_interval": settings.check_interval,
            "sticky_reset_secs": settings.sticky_reset_secs,
            "overbid_cancel_delay": settings.overbid_cancel_delay,
        },
    }

async def trading_loop():
    """Main trading loop - WebSocket driven with periodic tasks."""
    global orders

    print("Trading loop started (WebSocket mode)")

    # Connect WebSocket and register all callbacks
    if ws_client:
        ws_client.on_orderbook_change(on_orderbook_change)
        ws_client.on_fill(on_fill)
        ws_client.on_position_change(on_position_change)
        await ws_client.connect()

    # Start WebSocket listener in background
    ws_task = asyncio.create_task(ws_client.listen()) if ws_client else None

    # Periodic tasks (event time checks, state broadcast)
    # Fills and positions now come via WebSocket - no polling needed
    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)

            for match in matches.values():
                if not match.active:
                    continue

                # Check event time
                if match.event_time and now >= match.event_time:
                    print(f"[{match.id}] Event started - stopping")
                    match.active = False
                    await cancel_match_orders(match)
                    continue

            # Broadcast state periodically
            await broadcast(get_state())

        except Exception as e:
            print(f"Trading loop error: {e}")

        # Periodic check interval (for fills/inventory, not orderbook)
        await asyncio.sleep(settings.check_interval)

async def check_fills(match: Match):
    """Check for fills on a match."""
    global fills

    if not client:
        return

    for key in [(match.id, match.market_a.ticker, "yes"),
                (match.id, match.market_a.ticker, "no"),
                (match.id, match.market_b.ticker, "yes"),
                (match.id, match.market_b.ticker, "no")]:

        order_key = f"{key[0]}:{key[1]}:{key[2]}"
        if order_key not in orders:
            continue

        order = orders[order_key]

        try:
            resp = client.get(f"/portfolio/orders/{order.order_id}")
            order_data = resp.json().get("order", {})
            fill_count = order_data.get("fill_count", 0)
            remaining = order_data.get("remaining_count", order.count)

            new_fills = fill_count - order.filled
            if new_fills > 0:
                ticker, side = key[1], key[2]

                # Track cost basis
                fill_cost = new_fills * order.price
                if (ticker == match.market_a.ticker and side == "yes") or \
                   (ticker == match.market_b.ticker and side == "no"):
                    # Going long A
                    match.cost_long_a += fill_cost
                    match.count_long_a += new_fills
                else:
                    # Going long B
                    match.cost_long_b += fill_cost
                    match.count_long_b += new_fills

                # Record fill
                label = match.market_a.label if ticker == match.market_a.ticker else match.market_b.label
                fill = Fill(
                    timestamp=datetime.datetime.now().strftime("%H:%M:%S"),
                    match_id=match.id,
                    side=f"{label} {side.upper()}",
                    price=order.price,
                    count=new_fills,
                )
                fills.append(fill)

                print(f"[{match.id}] FILL: {fill.side} {new_fills}@{order.price}c | Inv: {match.inventory}")

                # Remove order if fully filled
                if remaining == 0:
                    del orders[order_key]
                else:
                    order.filled = fill_count
                    # Cancel partial
                    try:
                        client.cancel_order(order.order_id)
                        del orders[order_key]
                    except:
                        pass

        except Exception as e:
            pass

async def update_quotes(match: Match, book_a: dict, book_b: dict):
    """Update quotes for a match."""
    if not client:
        return

    theo_a = match.market_a.theo
    theo_b = match.market_b.theo
    edge = match.edge
    contracts = match.contracts
    inv_max = match.inventory_max

    # Inventory limits
    can_quote_a_yes = match.inventory < inv_max
    can_quote_b_no = match.inventory < inv_max
    can_quote_b_yes = match.inventory > -inv_max
    can_quote_a_no = match.inventory > -inv_max

    # Calculate breakeven ceilings for rebalancing
    # When overexposed to A, we can pay up to (100 - avg_cost_A) for B
    # When overexposed to B, we can pay up to (100 - avg_cost_B) for A
    avg_cost_a = match.cost_long_a / match.count_long_a if match.count_long_a > 0 else 0
    avg_cost_b = match.cost_long_b / match.count_long_b if match.count_long_b > 0 else 0
    # Use ceil on avg_cost to be conservative with fractional costs
    breakeven_for_b = 100 - math.ceil(avg_cost_a) - 1 - REBAL_FEE_BUFFER_CENTS if avg_cost_a > 0 else 0  # max we can pay for B
    breakeven_for_a = 100 - math.ceil(avg_cost_b) - 1 - REBAL_FEE_BUFFER_CENTS if avg_cost_b > 0 else 0  # max we can pay for A

    # When at inventory limit, use breakeven ceiling for rebalancing
    rebalance_ceiling_b = breakeven_for_b if match.inventory >= inv_max and breakeven_for_b > 0 else None
    rebalance_ceiling_a = breakeven_for_a if match.inventory <= -inv_max and breakeven_for_a > 0 else None

    # Calculate prices
    import time

    def get_order_info(order_key: str) -> tuple[Optional[int], bool]:
        """Returns (current_price, is_retest). Always returns price if we have order."""
        if order_key not in orders:
            return None, False
        order = orders[order_key]
        # Check if it's time for a retest (been at ceiling too long)
        is_retest = time.time() - order.placed_at > settings.sticky_reset_secs
        return order.price, is_retest

    label_a = match.market_a.label
    label_b = match.market_b.label

    # A YES (goes long A)
    a_yes_key = f"{match.id}:{match.market_a.ticker}:yes"
    ceiling_a = int(theo_a - edge)
    a_yes_our_price, a_yes_retest = get_order_info(a_yes_key)
    if rebalance_ceiling_a and rebalance_ceiling_a > theo_a - edge:
        # Overexposed to B, use breakeven ceiling to rebalance toward A
        a_yes_price = calculate_adaptive_price(
            rebalance_ceiling_a + edge, book_a["best_bid"], book_a["second_bid"], "bid", edge,
            a_yes_our_price, sticky_ceiling=True, is_retest=a_yes_retest,
            best_qty=book_a["best_bid_qty"], our_size=contracts,
            )
        if a_yes_price == -1:
            a_yes_reason = f"unprofitable rebalance ({book_a['best_bid']})"
        else:
            a_yes_reason = f"rebal ceiling {rebalance_ceiling_a}"
    elif can_quote_a_yes:
        a_yes_price = calculate_adaptive_price(
            theo_a, book_a["best_bid"], book_a["second_bid"], "bid", edge,
            a_yes_our_price, sticky_ceiling=True, is_retest=a_yes_retest,
            best_qty=book_a["best_bid_qty"], our_size=contracts,
            )
        if a_yes_price == -1:
            a_yes_reason = f"competitor overbidding ({book_a['best_bid']})"
        elif a_yes_price == ceiling_a:
            a_yes_reason = "at ceiling"
        else:
            a_yes_reason = None
    else:
        a_yes_price = -2
        a_yes_reason = f"overexposed to {label_a}"

    # B YES (goes long B)
    b_yes_key = f"{match.id}:{match.market_b.ticker}:yes"
    ceiling_b = int(theo_b - edge)
    b_yes_our_price, b_yes_retest = get_order_info(b_yes_key)
    if rebalance_ceiling_b and rebalance_ceiling_b > theo_b - edge:
        # Overexposed to A, use breakeven ceiling to rebalance toward B
        b_yes_price = calculate_adaptive_price(
            rebalance_ceiling_b + edge, book_b["best_bid"], book_b["second_bid"], "bid", edge,
            b_yes_our_price, sticky_ceiling=True, is_retest=b_yes_retest,
            best_qty=book_b["best_bid_qty"], our_size=contracts,
            )
        if b_yes_price == -1:
            b_yes_reason = f"unprofitable rebalance ({book_b['best_bid']})"
        else:
            b_yes_reason = f"rebal ceiling {rebalance_ceiling_b}"
    elif can_quote_b_yes:
        b_yes_price = calculate_adaptive_price(
            theo_b, book_b["best_bid"], book_b["second_bid"], "bid", edge,
            b_yes_our_price, sticky_ceiling=True, is_retest=b_yes_retest,
            best_qty=book_b["best_bid_qty"], our_size=contracts,
            )
        if b_yes_price == -1:
            b_yes_reason = f"competitor overbidding ({book_b['best_bid']})"
        elif b_yes_price == ceiling_b:
            b_yes_reason = "at ceiling"
        else:
            b_yes_reason = None
    else:
        b_yes_price = -2
        b_yes_reason = f"overexposed to {label_b}"

    # A NO (goes long B)
    a_no_key = f"{match.id}:{match.market_a.ticker}:no"
    a_no_our_price, a_no_retest = get_order_info(a_no_key)
    if rebalance_ceiling_b and rebalance_ceiling_b > theo_b - edge:
        # Overexposed to A, use breakeven ceiling to rebalance toward B
        a_no_price = calculate_adaptive_price(
            rebalance_ceiling_b + edge, book_a["best_no_bid"], book_a["second_no_bid"], "bid", edge,
            a_no_our_price, sticky_ceiling=True, is_retest=a_no_retest,
            best_qty=book_a["best_no_bid_qty"], our_size=contracts,
            )
        if a_no_price == -1:
            a_no_reason = f"unprofitable rebalance ({book_a['best_no_bid']})"
        else:
            a_no_reason = f"rebal ceiling {rebalance_ceiling_b}"
    elif can_quote_a_no:
        a_no_price = calculate_adaptive_price(
            theo_b, book_a["best_no_bid"], book_a["second_no_bid"], "bid", edge,
            a_no_our_price, sticky_ceiling=True, is_retest=a_no_retest,
            best_qty=book_a["best_no_bid_qty"], our_size=contracts,
            )
        if a_no_price == -1:
            a_no_reason = f"competitor overbidding ({book_a['best_no_bid']})"
        elif a_no_price == ceiling_b:
            a_no_reason = "at ceiling"
        else:
            a_no_reason = None
    else:
        a_no_price = -2
        a_no_reason = f"overexposed to {label_b}"

    # B NO (goes long A)
    b_no_key = f"{match.id}:{match.market_b.ticker}:no"
    b_no_our_price, b_no_retest = get_order_info(b_no_key)
    if rebalance_ceiling_a and rebalance_ceiling_a > theo_a - edge:
        # Overexposed to B, use breakeven ceiling to rebalance toward A
        b_no_price = calculate_adaptive_price(
            rebalance_ceiling_a + edge, book_b["best_no_bid"], book_b["second_no_bid"], "bid", edge,
            b_no_our_price, sticky_ceiling=True, is_retest=b_no_retest,
            best_qty=book_b["best_no_bid_qty"], our_size=contracts,
            )
        if b_no_price == -1:
            b_no_reason = f"unprofitable rebalance ({book_b['best_no_bid']})"
        else:
            b_no_reason = f"rebal ceiling {rebalance_ceiling_a}"
    elif can_quote_b_no:
        b_no_price = calculate_adaptive_price(
            theo_a, book_b["best_no_bid"], book_b["second_no_bid"], "bid", edge,
            b_no_our_price, sticky_ceiling=True, is_retest=b_no_retest,
            best_qty=book_b["best_no_bid_qty"], our_size=contracts,
            )
        if b_no_price == -1:
            b_no_reason = f"competitor overbidding ({book_b['best_no_bid']})"
        elif b_no_price == ceiling_a:
            b_no_reason = "at ceiling"
        else:
            b_no_reason = None
    else:
        b_no_price = -2
        b_no_reason = f"overexposed to {label_a}"

    # Place/update orders in parallel
    await asyncio.gather(
        place_or_update(match, match.market_a.ticker, "yes", True, a_yes_price, a_yes_key, contracts),
        place_or_update(match, match.market_b.ticker, "yes", True, b_yes_price, b_yes_key, contracts),
        place_or_update(match, match.market_a.ticker, "no", False, a_no_price, a_no_key, contracts),
        place_or_update(match, match.market_b.ticker, "no", False, b_no_price, b_no_key, contracts),
    )

    # Update order statuses (pass reason even with price for rebal detection)
    match.a_yes_status = OrderStatus(
        price=a_yes_price if a_yes_price > 0 else None,
        reason=a_yes_reason
    )
    match.a_no_status = OrderStatus(
        price=a_no_price if a_no_price > 0 else None,
        reason=a_no_reason
    )
    match.b_yes_status = OrderStatus(
        price=b_yes_price if b_yes_price > 0 else None,
        reason=b_yes_reason
    )
    match.b_no_status = OrderStatus(
        price=b_no_price if b_no_price > 0 else None,
        reason=b_no_reason
    )

async def place_or_update(match: Match, ticker: str, side: str, is_yes: bool,
                          target_price: int, order_key: str, contracts: int):
    """Place or update an order. Uses per-order-key lock to prevent race conditions."""
    global orders, overbid_since, order_locks, order_locks_lock
    import time

    if not client:
        return

    # Get or create lock for this order key
    async with order_locks_lock:
        if order_key not in order_locks:
            order_locks[order_key] = asyncio.Lock()
        lock = order_locks[order_key]

    # Use per-key lock - different orders can run in parallel,
    # but same order key is protected from concurrent access
    async with lock:
        current = orders.get(order_key)

        # Handle backing off (target_price == -1 for overbid, -2 for overexposed)
        if target_price < 0:
            # For overbid (-1), delay cancellation by overbid_cancel_delay seconds
            if target_price == -1 and current:
                now = time.time()
                if order_key not in overbid_since:
                    # First time seeing overbid - record timestamp, keep order
                    overbid_since[order_key] = now
                    return  # Keep order, don't cancel yet
                elif now - overbid_since[order_key] < settings.overbid_cancel_delay:
                    # Still within delay window - keep order
                    return
                # Delay expired - fall through to cancel

            # Cancel order (either overexposed, or overbid delay expired)
            if current:
                try:
                    await asyncio.to_thread(client.cancel_order, current.order_id)
                    del orders[order_key]
                except:
                    pass
            # Clear overbid tracking
            overbid_since.pop(order_key, None)
            return

        # Clear overbid tracking when we're back to quoting normally
        overbid_since.pop(order_key, None)

        # No change needed
        if current and current.price == target_price and current.count == contracts:
            return

        # Cancel existing
        if current:
            try:
                await asyncio.to_thread(client.cancel_order, current.order_id)
            except:
                pass

        # Place new
        exp_ts = int(match.event_time.timestamp()) if match.event_time else None
        result = await asyncio.to_thread(
            client.place_order,
            ticker=ticker,
            side="buy",
            is_yes=is_yes,
            price_cents=target_price,
            count=contracts,
            expiration_ts=exp_ts
        )

        order_id = result.get("order", {}).get("order_id")
        if order_id:
            orders[order_key] = OrderState(
                order_id=order_id,
                ticker=ticker,
                side=side,
                price=target_price,
                count=contracts,
                placed_at=time.time()
            )

async def cancel_match_orders(match: Match):
    """Cancel all orders for a match in parallel."""
    if not client:
        return

    keys_to_remove = [k for k in orders if k.startswith(f"{match.id}:")]

    async def cancel_one(key):
        try:
            await asyncio.to_thread(client.cancel_order, orders[key].order_id)
            del orders[key]
        except:
            pass

    await asyncio.gather(*[cancel_one(key) for key in keys_to_remove])

# =============================================================================
# FASTAPI APP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown."""
    global trading_task, order_locks_lock

    # Init locks
    order_locks_lock = asyncio.Lock()

    # Init P&L database
    pnl_db.init_db()

    # Init Kalshi client
    init_client()

    # Start trading loop
    trading_task = asyncio.create_task(trading_loop())

    yield

    # Shutdown - cancel ALL orders (from tracker AND from Kalshi)
    global _shutdown_done
    if _shutdown_done:
        print("\nShutdown already handled by emergency handler")
    else:
        _shutdown_done = True
        print("\nShutting down - cancelling all orders...")
        if client:
            # Collect order IDs from our tracker
            order_ids = set(order.order_id for order in orders.values())
            orders.clear()

            # Also fetch any resting orders from Kalshi we might have missed
            try:
                resp = client.get('/portfolio/orders?status=resting')
                for order in resp.json().get('orders', []):
                    order_ids.add(order['order_id'])
            except Exception as e:
                print(f"  Warning: couldn't fetch resting orders: {e}")

            # Cancel all in parallel
            if order_ids:
                print(f"  Cancelling {len(order_ids)} orders...")

                def cancel_one(order_id):
                    try:
                        client.cancel_order(order_id)
                        return True
                    except:
                        return False

                with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                    results = list(executor.map(cancel_one, order_ids))
                    cancelled = sum(results)
                    print(f"  Cancelled {cancelled}/{len(order_ids)} orders")
            else:
                print("  No orders to cancel")

    if trading_task:
        trading_task.cancel()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def index():
    return FileResponse("static/index.html")

@app.get("/api/state")
async def api_state():
    return get_state()

@app.post("/api/matches")
async def api_add_match(config: MatchConfig):
    match = await asyncio.to_thread(add_match, config)
    await broadcast(get_state())
    return {"id": match.id, "name": match.name}

@app.post("/api/matches/batch")
async def api_add_matches_batch(configs: list[MatchConfig]):
    """Add multiple matches in parallel."""
    # Add all matches in parallel using thread pool
    results = await asyncio.gather(*[
        asyncio.to_thread(add_match, config) for config in configs
    ])
    await broadcast(get_state())
    return {"added": len(results), "ids": [m.id for m in results]}

@app.post("/api/matches/{match_id}/start")
async def api_start_match(match_id: str):
    if match_id in matches:
        match = matches[match_id]
        match.active = True
        # Subscribe to orderbook WebSocket
        if ws_client:
            await ws_client.subscribe([match.market_a.ticker, match.market_b.ticker])
        # Immediately place initial quotes (don't wait for WebSocket update)
        await handle_match_update(match)
    return {"ok": True}

@app.post("/api/matches/{match_id}/stop")
async def api_stop_match(match_id: str):
    if match_id in matches:
        match = matches[match_id]
        match.active = False
        await cancel_match_orders(match)
        # Unsubscribe from orderbook WebSocket
        if ws_client:
            await ws_client.unsubscribe([match.market_a.ticker, match.market_b.ticker])
        await broadcast(get_state())
    return {"ok": True}

@app.post("/api/matches/start-all")
async def api_start_all_matches():
    """Start all inactive matches in parallel."""
    inactive = [m for m in matches.values() if not m.active]
    if not inactive:
        return {"ok": True, "started": 0}

    # Mark all as active and subscribe to WebSocket
    tickers = []
    for match in inactive:
        match.active = True
        tickers.extend([match.market_a.ticker, match.market_b.ticker])

    if ws_client and tickers:
        await ws_client.subscribe(tickers)

    # Place initial quotes for all matches in parallel
    await asyncio.gather(*[handle_match_update(m) for m in inactive])

    return {"ok": True, "started": len(inactive)}

@app.post("/api/matches/{match_id}/odds")
async def api_update_odds(match_id: str, update: UpdateOdds):
    update_match_odds(match_id, update.odds_a, update.odds_b)
    await broadcast(get_state())
    return {"ok": True}

@app.post("/api/matches/{match_id}/refresh-odds")
async def api_refresh_odds(match_id: str):
    """Refresh odds for a match from Odds API."""
    result = refresh_match_odds(match_id)
    await broadcast(get_state())
    return result

@app.post("/api/matches/{match_id}/settings")
async def api_update_settings(match_id: str, update: UpdateSettings):
    if match_id not in matches:
        return {"ok": False}

    match = matches[match_id]
    needs_quote_update = False

    if update.odds_a is not None and update.odds_b is not None:
        update_match_odds(match_id, update.odds_a, update.odds_b)
        needs_quote_update = True
    if update.edge is not None:
        print(f"[{match_id}] Edge changed: {match.edge} -> {update.edge}")
        match.edge = update.edge
        needs_quote_update = True
    if update.contracts is not None:
        match.contracts = update.contracts
        needs_quote_update = True
    if update.inventory_max is not None:
        match.inventory_max = update.inventory_max
        needs_quote_update = True
    if update.inventory is not None:
        match.inventory = update.inventory
        needs_quote_update = True

    # Immediately update quotes if match is active (cancels overexposing orders)
    if needs_quote_update and match.active:
        await handle_match_update(match)

    await broadcast(get_state())
    return {"ok": True}

@app.delete("/api/matches/{match_id}")
async def api_remove_match(match_id: str):
    if match_id in matches:
        await cancel_match_orders(matches[match_id])
        del matches[match_id]
        await broadcast(get_state())
    return {"ok": True}

@app.delete("/api/matches/all")
async def api_remove_all_matches():
    """Remove all matches."""
    for match_id in list(matches.keys()):
        await cancel_match_orders(matches[match_id])
        del matches[match_id]
    await broadcast(get_state())
    return {"ok": True}

class UpdateGlobalSettings(BaseModel):
    """Update global settings."""
    check_interval: Optional[float] = None
    sticky_reset_secs: Optional[float] = None
    overbid_cancel_delay: Optional[float] = None

@app.post("/api/settings")
async def api_update_settings(update: UpdateGlobalSettings):
    """Update global timing settings."""
    if update.check_interval is not None:
        settings.check_interval = max(0.5, update.check_interval)  # min 0.5s
    if update.sticky_reset_secs is not None:
        settings.sticky_reset_secs = max(1.0, update.sticky_reset_secs)
    if update.overbid_cancel_delay is not None:
        settings.overbid_cancel_delay = max(1.0, update.overbid_cancel_delay)

    await broadcast(get_state())
    return {"ok": True, "settings": {
        "check_interval": settings.check_interval,
        "sticky_reset_secs": settings.sticky_reset_secs,
        "overbid_cancel_delay": settings.overbid_cancel_delay,
    }}

@app.post("/api/kill")
async def api_kill_all():
    """Cancel all orders and stop all matches - FAST parallel cancellation."""
    global orders
    import concurrent.futures

    # Stop all matches immediately
    for match in matches.values():
        match.active = False

    # Collect all order IDs to cancel
    order_ids = set()

    # From our tracker
    for order_key, order in list(orders.items()):
        order_ids.add(order.order_id)
    orders.clear()

    # From Kalshi's resting orders
    try:
        resp = client.get('/portfolio/orders?status=resting')
        for order in resp.json().get('orders', []):
            order_ids.add(order['order_id'])
    except:
        pass

    # Cancel all in parallel using thread pool
    cancelled = 0
    if order_ids:
        def cancel_one(order_id):
            try:
                client.cancel_order(order_id)
                return True
            except:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(cancel_one, order_ids))
            cancelled = sum(results)

    await broadcast(get_state())
    return {"ok": True, "cancelled": cancelled}

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

@app.post("/api/sync-inventory")
async def api_sync_inventory():
    """Sync inventory for all matches from Kalshi positions."""
    updated = []
    for match in matches.values():
        old_inv = match.inventory
        match.inventory = await asyncio.to_thread(calculate_match_inventory, match)
        if match.inventory != old_inv:
            updated.append({"id": match.id, "old": old_inv, "new": match.inventory})
            print(f"[{match.id}] Inventory synced: {old_inv} -> {match.inventory}")
    await broadcast(get_state())
    return {"ok": True, "updated": updated}

@app.get("/api/markets/lacrosse")
async def api_lacrosse_markets():
    """Get available lacrosse markets with odds."""
    try:
        matches = scan_lacrosse()
        return {
            "markets": [
                {
                    "home_team": m.home_team,
                    "away_team": m.away_team,
                    "ticker_a": m.ticker_away,  # Away team is ticker A
                    "ticker_b": m.ticker_home,  # Home team is ticker B
                    "odds_a": round(m.fair_odds_away, 2),
                    "odds_b": round(m.fair_odds_home, 2),
                    "theo_a": m.theo_away,
                    "theo_b": m.theo_home,
                    "bookmakers": m.bookmakers,
                    "commence_time": m.commence_time,
                }
                for m in matches
            ]
        }
    except Exception as e:
        print(f"Error fetching lacrosse markets: {e}")
        return {"markets": [], "error": str(e)}

@app.get("/api/markets/boxing")
async def api_boxing_markets():
    """Get available boxing markets with odds."""
    try:
        matches = scan_boxing()
        return {
            "markets": [
                {
                    "home_team": m.fighter_b,  # Fighter B is "home"
                    "away_team": m.fighter_a,  # Fighter A is "away"
                    "ticker_a": m.ticker_a,
                    "ticker_b": m.ticker_b,
                    "odds_a": round(m.fair_odds_a, 2),
                    "odds_b": round(m.fair_odds_b, 2),
                    "theo_a": m.theo_a,
                    "theo_b": m.theo_b,
                    "bookmakers": m.bookmakers,
                    "commence_time": m.commence_time,
                }
                for m in matches
            ]
        }
    except Exception as e:
        print(f"Error fetching boxing markets: {e}")
        return {"markets": [], "error": str(e)}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    websockets.append(ws)

    # Send initial state
    await ws.send_text(json.dumps(get_state(), default=str))

    try:
        while True:
            # Keep connection alive, handle incoming messages
            data = await ws.receive_text()
            # Could handle commands here
    except Exception:
        if ws in websockets:
            websockets.remove(ws)

# Mount static files
import os
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
