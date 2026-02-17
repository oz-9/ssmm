"""
Multi-market dashboard for Kalshi market making.
Run: python dashboard.py
Open: http://localhost:8000
"""

import asyncio
import datetime
import json
import math
from dataclasses import dataclass, asdict
from typing import Optional
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Import from mm.py
from mm import (
    KalshiClient,
    get_book_with_depth,
    calculate_adaptive_price,
    calculate_theo,
    get_event_start_time,
    OrderState,
    KALSHI_BASE_URL,
)
from lacrosse_scanner import scan as scan_lacrosse
from boxing_scanner import scan as scan_boxing

# =============================================================================
# RATE TRACKING & SETTINGS
# =============================================================================

import time
from collections import deque

# Request tracking (rolling window)
request_timestamps: deque = deque(maxlen=1000)  # Store last 1000 request times
RATE_WINDOW = 10.0  # seconds to measure rate over
REBAL_FEE_BUFFER_CENTS = 2  # Maker fees: ~1c entry + ~1c rebalance (ceil(0.0175 * P * (1-P)) per leg)

def track_request():
    """Record a request timestamp."""
    request_timestamps.append(time.time())

def get_request_rate() -> dict:
    """Get current request rate stats."""
    now = time.time()
    # Count requests in the last RATE_WINDOW seconds
    recent = sum(1 for t in request_timestamps if now - t < RATE_WINDOW)
    rate_per_sec = recent / RATE_WINDOW

    # Estimate limit (Kalshi typically allows ~10 req/sec for trading)
    estimated_limit = 10.0  # requests per second
    usage_pct = (rate_per_sec / estimated_limit) * 100

    return {
        "requests_last_10s": recent,
        "rate_per_sec": round(rate_per_sec, 2),
        "estimated_limit": estimated_limit,
        "usage_pct": min(round(usage_pct, 1), 100.0),
    }

# Global timing settings
class Settings:
    check_interval: float = 2.0      # How often to check/update quotes (seconds)
    sticky_reset_secs: float = 10.0  # How long to stay at ceiling before retesting
    overbid_cancel_delay: float = 10.0  # How long to wait before cancelling when overbid

settings = Settings()

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

# =============================================================================
# GLOBAL STATE
# =============================================================================

matches: dict[str, Match] = {}
fills: list[Fill] = []
orders: dict[str, OrderState] = {}  # (match_id, ticker, side) -> OrderState
overbid_since: dict[str, float] = {}  # order_key -> timestamp when overbid first detected
client: Optional[KalshiClient] = None
websockets: list[WebSocket] = []
trading_task: Optional[asyncio.Task] = None

# Config (now using settings object, these are kept for reference)
# CHECK_INTERVAL = settings.check_interval
# OVERBID_CANCEL_DELAY = settings.overbid_cancel_delay

# =============================================================================
# KALSHI CLIENT
# =============================================================================

def init_client():
    """Initialize Kalshi client from config."""
    global client
    try:
        from config.config import KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH
        client = KalshiClient(KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH)
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

def add_match(config: MatchConfig) -> Match:
    """Add a new match to track."""
    theo = calculate_theo(config.odds_a, config.odds_b)

    label_a = get_label(config.ticker_a)
    label_b = get_label(config.ticker_b)

    match_id = f"{label_a}v{label_b}"
    name = config.name or f"{label_a} vs {label_b}"

    match = Match(
        id=match_id,
        name=name,
        market_a=Market(ticker=config.ticker_a, label=label_a, theo=theo["a"]),
        market_b=Market(ticker=config.ticker_b, label=label_b, theo=theo["b"]),
        odds_a=config.odds_a,
        odds_b=config.odds_b,
        event_time=get_event_start_time(config.ticker_a),
        edge=config.edge,
        contracts=config.contracts,
        inventory_max=config.inventory_max,
    )

    # Fetch current positions from Kalshi
    match.inventory = calculate_match_inventory(match)

    matches[match_id] = match
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

def refresh_orderbooks():
    """Refresh orderbook data for all matches."""
    for match in matches.values():
        # Market A
        book_a = get_book_with_depth(match.market_a.ticker)
        match.market_a.best_bid = book_a["best_bid"]
        match.market_a.best_ask = book_a["best_ask"]

        # Market B
        book_b = get_book_with_depth(match.market_b.ticker)
        match.market_b.best_bid = book_b["best_bid"]
        match.market_b.best_ask = book_b["best_ask"]

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
                "active": m.active,
                "inventory": m.inventory,
                "market_a": {
                    "label": m.market_a.label,
                    "ticker": m.market_a.ticker,
                    "theo": m.market_a.theo,
                    "best_bid": m.market_a.best_bid,
                    "best_ask": m.market_a.best_ask,
                },
                "market_b": {
                    "label": m.market_b.label,
                    "ticker": m.market_b.ticker,
                    "theo": m.market_b.theo,
                    "best_bid": m.market_b.best_bid,
                    "best_ask": m.market_b.best_ask,
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
            }
            for m in matches.values()
        ],
        "fills": [asdict(f) for f in fills[-10:]],  # Last 10 fills
        "rate": get_request_rate(),
        "settings": {
            "check_interval": settings.check_interval,
            "sticky_reset_secs": settings.sticky_reset_secs,
            "overbid_cancel_delay": settings.overbid_cancel_delay,
        },
    }

async def trading_loop():
    """Main trading loop - runs in background."""
    global orders

    print("Trading loop started")

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
                    # Cancel orders for this match
                    await cancel_match_orders(match)
                    continue

                # Refresh orderbooks
                book_a = get_book_with_depth(match.market_a.ticker)
                track_request()
                book_b = get_book_with_depth(match.market_b.ticker)
                track_request()

                match.market_a.best_bid = book_a["best_bid"]
                match.market_a.best_ask = book_a["best_ask"]
                match.market_b.best_bid = book_b["best_bid"]
                match.market_b.best_ask = book_b["best_ask"]

                # Sync inventory from Kalshi positions
                match.inventory = calculate_match_inventory(match)

                # Check for fills (for display)
                await check_fills(match)

                # Update quotes
                await update_quotes(match, book_a, book_b)

            # Broadcast state
            await broadcast(get_state())

        except Exception as e:
            print(f"Trading loop error: {e}")

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

    # Calculate prices - drop to ceiling periodically based on settings
    import time

    def get_our_price(order_key: str) -> Optional[int]:
        if order_key not in orders:
            return None
        order = orders[order_key]
        # If order is older than sticky_reset_secs, return None to force recalc from ceiling
        if time.time() - order.placed_at > settings.sticky_reset_secs:
            return None
        return order.price

    label_a = match.market_a.label
    label_b = match.market_b.label

    # A YES (goes long A)
    a_yes_key = f"{match.id}:{match.market_a.ticker}:yes"
    ceiling_a = int(theo_a - edge)
    if rebalance_ceiling_a and rebalance_ceiling_a > theo_a - edge:
        # Overexposed to B, use breakeven ceiling to rebalance toward A
        a_yes_price = calculate_adaptive_price(
            rebalance_ceiling_a + edge, book_a["best_bid"], book_a["second_bid"], "bid", edge,
            get_our_price(a_yes_key), sticky_ceiling=True,
            best_qty=book_a["best_bid_qty"], our_size=contracts,
            )
        if a_yes_price == -1:
            a_yes_reason = f"unprofitable rebalance ({book_a['best_bid']})"
        else:
            a_yes_reason = f"rebal ceiling {rebalance_ceiling_a}"
    elif can_quote_a_yes:
        a_yes_price = calculate_adaptive_price(
            theo_a, book_a["best_bid"], book_a["second_bid"], "bid", edge,
            get_our_price(a_yes_key), sticky_ceiling=True,
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
    if rebalance_ceiling_b and rebalance_ceiling_b > theo_b - edge:
        # Overexposed to A, use breakeven ceiling to rebalance toward B
        b_yes_price = calculate_adaptive_price(
            rebalance_ceiling_b + edge, book_b["best_bid"], book_b["second_bid"], "bid", edge,
            get_our_price(b_yes_key), sticky_ceiling=True,
            best_qty=book_b["best_bid_qty"], our_size=contracts,
            )
        if b_yes_price == -1:
            b_yes_reason = f"unprofitable rebalance ({book_b['best_bid']})"
        else:
            b_yes_reason = f"rebal ceiling {rebalance_ceiling_b}"
    elif can_quote_b_yes:
        b_yes_price = calculate_adaptive_price(
            theo_b, book_b["best_bid"], book_b["second_bid"], "bid", edge,
            get_our_price(b_yes_key), sticky_ceiling=True,
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
    if rebalance_ceiling_b and rebalance_ceiling_b > theo_b - edge:
        # Overexposed to A, use breakeven ceiling to rebalance toward B
        a_no_price = calculate_adaptive_price(
            rebalance_ceiling_b + edge, book_a["best_no_bid"], book_a["second_no_bid"], "bid", edge,
            get_our_price(a_no_key), sticky_ceiling=True,
            best_qty=book_a["best_no_bid_qty"], our_size=contracts,
            )
        if a_no_price == -1:
            a_no_reason = f"unprofitable rebalance ({book_a['best_no_bid']})"
        else:
            a_no_reason = f"rebal ceiling {rebalance_ceiling_b}"
    elif can_quote_a_no:
        a_no_price = calculate_adaptive_price(
            theo_b, book_a["best_no_bid"], book_a["second_no_bid"], "bid", edge,
            get_our_price(a_no_key), sticky_ceiling=True,
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
    if rebalance_ceiling_a and rebalance_ceiling_a > theo_a - edge:
        # Overexposed to B, use breakeven ceiling to rebalance toward A
        b_no_price = calculate_adaptive_price(
            rebalance_ceiling_a + edge, book_b["best_no_bid"], book_b["second_no_bid"], "bid", edge,
            get_our_price(b_no_key), sticky_ceiling=True,
            best_qty=book_b["best_no_bid_qty"], our_size=contracts,
            )
        if b_no_price == -1:
            b_no_reason = f"unprofitable rebalance ({book_b['best_no_bid']})"
        else:
            b_no_reason = f"rebal ceiling {rebalance_ceiling_a}"
    elif can_quote_b_no:
        b_no_price = calculate_adaptive_price(
            theo_a, book_b["best_no_bid"], book_b["second_no_bid"], "bid", edge,
            get_our_price(b_no_key), sticky_ceiling=True,
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

    # Place/update orders
    await place_or_update(match, match.market_a.ticker, "yes", True, a_yes_price, a_yes_key, contracts)
    await place_or_update(match, match.market_b.ticker, "yes", True, b_yes_price, b_yes_key, contracts)
    await place_or_update(match, match.market_a.ticker, "no", False, a_no_price, a_no_key, contracts)
    await place_or_update(match, match.market_b.ticker, "no", False, b_no_price, b_no_key, contracts)

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
    """Place or update an order."""
    global orders, overbid_since
    import time

    if not client:
        return

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
                client.cancel_order(current.order_id)
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
            client.cancel_order(current.order_id)
        except:
            pass

    # Place new
    exp_ts = int(match.event_time.timestamp()) if match.event_time else None
    result = client.place_order(
        ticker=ticker,
        side="buy",
        is_yes=is_yes,
        price_cents=target_price,
        count=contracts,
        expiration_ts=exp_ts
    )

    order_id = result.get("order", {}).get("order_id")
    if order_id:
        import time
        orders[order_key] = OrderState(
            order_id=order_id,
            ticker=ticker,
            side=side,
            price=target_price,
            count=contracts,
            placed_at=time.time()
        )

async def cancel_match_orders(match: Match):
    """Cancel all orders for a match."""
    if not client:
        return

    keys_to_remove = [k for k in orders if k.startswith(f"{match.id}:")]
    for key in keys_to_remove:
        try:
            client.cancel_order(orders[key].order_id)
            del orders[key]
        except:
            pass

# =============================================================================
# FASTAPI APP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown."""
    global trading_task

    # Init Kalshi client
    init_client()

    # Start trading loop
    trading_task = asyncio.create_task(trading_loop())

    yield

    # Shutdown - cancel all orders
    print("\nShutting down - cancelling all orders...")
    if client:
        for order_key, order in list(orders.items()):
            try:
                client.cancel_order(order.order_id)
                print(f"  Cancelled {order_key}")
            except Exception as e:
                print(f"  Failed to cancel {order_key}: {e}")

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
    match = add_match(config)
    await broadcast(get_state())
    return {"id": match.id, "name": match.name}

@app.post("/api/matches/{match_id}/start")
async def api_start_match(match_id: str):
    if match_id in matches:
        matches[match_id].active = True
        await broadcast(get_state())
    return {"ok": True}

@app.post("/api/matches/{match_id}/stop")
async def api_stop_match(match_id: str):
    if match_id in matches:
        matches[match_id].active = False
        await cancel_match_orders(matches[match_id])
        await broadcast(get_state())
    return {"ok": True}

@app.post("/api/matches/{match_id}/odds")
async def api_update_odds(match_id: str, update: UpdateOdds):
    update_match_odds(match_id, update.odds_a, update.odds_b)
    await broadcast(get_state())
    return {"ok": True}

@app.post("/api/matches/{match_id}/settings")
async def api_update_settings(match_id: str, update: UpdateSettings):
    if match_id not in matches:
        return {"ok": False}

    match = matches[match_id]

    if update.odds_a is not None and update.odds_b is not None:
        update_match_odds(match_id, update.odds_a, update.odds_b)
    if update.edge is not None:
        print(f"[{match_id}] Edge changed: {match.edge} -> {update.edge}")
        match.edge = update.edge
    if update.contracts is not None:
        match.contracts = update.contracts
    if update.inventory_max is not None:
        match.inventory_max = update.inventory_max
    if update.inventory is not None:
        match.inventory = update.inventory

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
    except WebSocketDisconnect:
        websockets.remove(ws)

# Mount static files
import os
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
