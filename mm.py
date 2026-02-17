"""
Generalized Market Maker for Kalshi binary markets.
Works with any two-outcome event.
"""

import requests
import base64
import datetime
import time
import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Optional
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

try:
    import websockets
except ImportError:
    websockets = None


KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


# =============================================================================
# KALSHI CLIENT
# =============================================================================

class KalshiClient:
    def __init__(self, key_id: str, private_key_path: str):
        self.key_id = key_id
        self.base_url = KALSHI_BASE_URL

        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

    def _sign(self, message: str) -> str:
        signature = self.private_key.sign(
            message.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')

    def _headers(self, method: str, path: str) -> dict:
        timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
        full_path = f"/trade-api/v2{path}".split('?')[0]
        signature = self._sign(f"{timestamp}{method}{full_path}")
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json"
        }

    def get(self, path: str):
        url = f"{self.base_url}{path}"
        return requests.get(url, headers=self._headers("GET", path))

    def post(self, path: str, data: dict):
        url = f"{self.base_url}{path}"
        return requests.post(url, headers=self._headers("POST", path), json=data)

    def delete(self, path: str):
        url = f"{self.base_url}{path}"
        return requests.delete(url, headers=self._headers("DELETE", path))

    def get_balance(self) -> dict:
        resp = self.get("/portfolio/balance")
        return resp.json()

    def get_market(self, ticker: str) -> dict:
        resp = requests.get(f"{self.base_url}/markets/{ticker}")
        return resp.json().get("market", {})

    def place_order(self, ticker: str, side: str, is_yes: bool, price_cents: int, count: int,
                    expiration_ts: Optional[int] = None) -> dict:
        order = {
            "ticker": ticker,
            "action": side,
            "side": "yes" if is_yes else "no",
            "type": "limit",
            "count": count,
        }
        if is_yes:
            order["yes_price"] = price_cents
        else:
            order["no_price"] = price_cents
        if expiration_ts:
            order["expiration_ts"] = expiration_ts
        resp = self.post("/portfolio/orders", order)
        return resp.json()

    def cancel_order(self, order_id: str) -> dict:
        resp = self.delete(f"/portfolio/orders/{order_id}")
        return resp.json()

    def get_orders(self, ticker: str = None, status: str = "resting") -> list:
        path = f"/portfolio/orders?status={status}"
        if ticker:
            path += f"&ticker={ticker}"
        resp = self.get(path)
        return resp.json().get("orders", [])

    def cancel_all_orders(self, ticker: str = None) -> int:
        orders = self.get_orders(ticker=ticker, status="resting")
        cancelled = 0
        for order in orders:
            self.cancel_order(order["order_id"])
            cancelled += 1
        return cancelled

    def get_positions(self, ticker: str = None) -> list:
        path = "/portfolio/positions"
        if ticker:
            path += f"?ticker={ticker}"
        resp = self.get(path)
        return resp.json().get("market_positions", [])


# =============================================================================
# KALSHI WEBSOCKET
# =============================================================================

class KalshiWebSocket:
    """WebSocket client for Kalshi orderbook streams."""

    WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    def __init__(self, key_id: str, private_key_path: str):
        self.key_id = key_id
        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )
        self.ws = None
        self.orderbooks: dict[str, dict] = {}  # ticker -> {yes: [[price, qty]], no: [[price, qty]]}
        self.subscribed_tickers: set[str] = set()
        self._message_id = 0
        self._callbacks: list = []  # list of async callbacks on orderbook change

    def _sign(self, message: str) -> str:
        signature = self.private_key.sign(
            message.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')

    def _auth_headers(self) -> dict:
        timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
        path = "/trade-api/ws/v2"
        signature = self._sign(f"{timestamp}GET{path}")
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    async def connect(self):
        """Establish WebSocket connection."""
        if websockets is None:
            raise ImportError("websockets package is required. Install with: pip install websockets")
        headers = self._auth_headers()
        self.ws = await websockets.connect(self.WS_URL, additional_headers=headers)
        print("Kalshi WebSocket connected")

    async def subscribe(self, tickers: list[str]):
        """Subscribe to orderbook updates for tickers."""
        if not self.ws:
            await self.connect()

        new_tickers = [t for t in tickers if t not in self.subscribed_tickers]
        if not new_tickers:
            return

        self._message_id += 1
        msg = {
            "id": self._message_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": new_tickers
            }
        }
        await self.ws.send(json.dumps(msg))
        self.subscribed_tickers.update(new_tickers)
        print(f"Subscribed to orderbook: {new_tickers}")

    async def unsubscribe(self, tickers: list[str]):
        """Unsubscribe from orderbook updates."""
        if not self.ws:
            return

        to_unsub = [t for t in tickers if t in self.subscribed_tickers]
        if not to_unsub:
            return

        self._message_id += 1
        msg = {
            "id": self._message_id,
            "cmd": "unsubscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": to_unsub
            }
        }
        await self.ws.send(json.dumps(msg))
        self.subscribed_tickers -= set(to_unsub)
        for t in to_unsub:
            self.orderbooks.pop(t, None)
        print(f"Unsubscribed from orderbook: {to_unsub}")

    def on_orderbook_change(self, callback):
        """Register callback for orderbook changes. callback(ticker, book_data)"""
        self._callbacks.append(callback)

    def _parse_book(self, ticker: str) -> dict:
        """Convert internal orderbook to get_book_with_depth format."""
        book = self.orderbooks.get(ticker, {"yes": [], "no": []})

        yes_bids = sorted(book.get("yes", []), key=lambda x: x[0], reverse=True)
        no_bids = sorted(book.get("no", []), key=lambda x: x[0], reverse=True)

        best_yes_bid = yes_bids[0][0] if yes_bids else 0
        best_yes_bid_qty = yes_bids[0][1] if yes_bids else 0
        second_yes_bid = yes_bids[1][0] if len(yes_bids) > 1 else 0

        best_no_bid = no_bids[0][0] if no_bids else 0
        best_no_bid_qty = no_bids[0][1] if no_bids else 0
        second_no_bid = no_bids[1][0] if len(no_bids) > 1 else 0

        best_yes_ask = 100 - best_no_bid if best_no_bid > 0 else 100

        return {
            "best_bid": best_yes_bid,
            "best_bid_qty": best_yes_bid_qty,
            "second_bid": second_yes_bid,
            "best_ask": best_yes_ask,
            "second_ask": 100 - second_no_bid if second_no_bid > 0 else 100,
            "best_no_bid": best_no_bid,
            "best_no_bid_qty": best_no_bid_qty,
            "second_no_bid": second_no_bid,
        }

    def get_book(self, ticker: str) -> dict:
        """Get current orderbook for ticker in get_book_with_depth format."""
        return self._parse_book(ticker)

    async def _handle_message(self, msg: dict):
        """Process incoming WebSocket message."""
        msg_type = msg.get("type")

        if msg_type == "orderbook_snapshot":
            ticker = msg.get("msg", {}).get("market_ticker")
            if ticker:
                self.orderbooks[ticker] = {
                    "yes": [[lvl[0], lvl[1]] for lvl in msg["msg"].get("yes", [])],
                    "no": [[lvl[0], lvl[1]] for lvl in msg["msg"].get("no", [])]
                }
                for cb in self._callbacks:
                    await cb(ticker, self._parse_book(ticker))

        elif msg_type == "orderbook_delta":
            data = msg.get("msg", {})
            ticker = data.get("market_ticker")
            price = data.get("price")
            delta = data.get("delta")
            side = data.get("side")

            if ticker and price is not None and delta is not None and side:
                if ticker not in self.orderbooks:
                    self.orderbooks[ticker] = {"yes": [], "no": []}

                book_side = self.orderbooks[ticker][side]
                # Find existing price level
                found = False
                for i, (p, q) in enumerate(book_side):
                    if p == price:
                        new_qty = q + delta
                        if new_qty <= 0:
                            book_side.pop(i)
                        else:
                            book_side[i] = [price, new_qty]
                        found = True
                        break

                if not found and delta > 0:
                    book_side.append([price, delta])

                for cb in self._callbacks:
                    await cb(ticker, self._parse_book(ticker))

    async def listen(self):
        """Main loop to receive and process messages."""
        if not self.ws:
            await self.connect()

        try:
            async for message in self.ws:
                try:
                    msg = json.loads(message)
                    await self._handle_message(msg)
                except json.JSONDecodeError:
                    pass
        except websockets.ConnectionClosed:
            print("WebSocket disconnected, reconnecting...")
            await self.reconnect()

    async def reconnect(self):
        """Reconnect and resubscribe."""
        await asyncio.sleep(1)
        tickers = list(self.subscribed_tickers)
        self.subscribed_tickers.clear()
        self.orderbooks.clear()
        await self.connect()
        if tickers:
            await self.subscribe(tickers)

    async def close(self):
        """Close WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.ws = None


# =============================================================================
# HELPERS
# =============================================================================

@dataclass
class OrderState:
    order_id: str
    ticker: str
    side: str
    price: int
    count: int
    filled: int = 0
    placed_at: float = 0.0  # timestamp when placed


def get_book_with_depth(ticker: str) -> dict:
    url = f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook"
    resp = requests.get(url)
    orderbook = resp.json().get("orderbook", {})

    yes_bids = orderbook.get("yes") or []
    no_bids = orderbook.get("no") or []

    yes_bids_sorted = sorted(yes_bids, key=lambda x: x[0], reverse=True)
    no_bids_sorted = sorted(no_bids, key=lambda x: x[0], reverse=True)

    best_yes_bid = yes_bids_sorted[0][0] if yes_bids_sorted else 0
    best_yes_bid_qty = yes_bids_sorted[0][1] if yes_bids_sorted else 0
    second_yes_bid = yes_bids_sorted[1][0] if len(yes_bids_sorted) > 1 else 0

    best_no_bid = no_bids_sorted[0][0] if no_bids_sorted else 0
    best_no_bid_qty = no_bids_sorted[0][1] if no_bids_sorted else 0
    second_no_bid = no_bids_sorted[1][0] if len(no_bids_sorted) > 1 else 0

    best_yes_ask = 100 - best_no_bid if best_no_bid > 0 else 100
    second_yes_ask = 100 - second_no_bid if second_no_bid > 0 else 100

    return {
        "best_bid": best_yes_bid,
        "best_bid_qty": best_yes_bid_qty,
        "second_bid": second_yes_bid,
        "best_ask": best_yes_ask,
        "second_ask": second_yes_ask,
        "best_no_bid": best_no_bid,
        "best_no_bid_qty": best_no_bid_qty,
        "second_no_bid": second_no_bid,
    }


def calculate_theo(odds_a: float, odds_b: float) -> dict:
    """Calculate no-vig theo from decimal odds."""
    prob_a = 1 / odds_a if odds_a > 0 else 0
    prob_b = 1 / odds_b if odds_b > 0 else 0
    total = prob_a + prob_b

    theo_a = (prob_a / total) * 100
    theo_b = (prob_b / total) * 100

    return {
        "a": round(theo_a, 1),
        "b": round(theo_b, 1),
        "vig": round((total - 1) * 100, 2)
    }


def calculate_adaptive_price(
    theo: float, best_price: int, second_price: int, side: str,
    edge_min: float = 1.0, our_current: Optional[int] = None,
    sticky_ceiling: bool = False, is_retest: bool = False,
    best_qty: int = 0, our_size: int = 0
) -> int:
    """
    Calculate adaptive price within theo ceiling.

    Args:
        theo: theo probability in cents
        best_price: current best bid/ask in book
        second_price: second best (competition if we're at best)
        side: "bid" or "ask"
        edge_min: minimum edge from theo
        our_current: our current price (to detect our order)
        sticky_ceiling: stay at ceiling even if competition drops
        is_retest: drop down to find better price (overrides sticky)
        best_qty: quantity at best price (to detect ties)
        our_size: our order size

    Returns:
        Target price, or -1 if should back off
    """
    if side == "bid":
        ceiling = int(theo - edge_min)

        if our_current is not None and best_price == our_current:
            tied_at_top = best_qty > our_size
            if tied_at_top and our_current < ceiling:
                target = our_current + 1
            elif sticky_ceiling and not is_retest:
                target = our_current
            else:
                target = second_price + 1 if second_price > 0 else 1
        elif our_current is not None and best_price > our_current:
            if best_price > ceiling:
                return -1
            target = best_price + 1
        else:
            if best_price > ceiling:
                return -1
            target = best_price + 1

        return max(1, min(ceiling, target))
    else:
        floor = int(theo + edge_min) + 1

        if our_current is not None and best_price == our_current:
            tied_at_top = best_qty > our_size
            if tied_at_top and our_current > floor:
                target = our_current - 1
            elif sticky_ceiling and not is_retest:
                target = our_current
            else:
                target = second_price - 1 if second_price < 100 else 99
        elif our_current is not None and best_price < our_current:
            if best_price < floor:
                if must_quote:
                    return min(99, floor)
                return -1
            target = best_price - 1
        else:
            if best_price < floor:
                if must_quote:
                    return min(99, floor)
                return -1
            target = best_price - 1

        return max(floor, min(99, target))


def get_event_start_time(ticker: str) -> Optional[datetime.datetime]:
    url = f"{KALSHI_BASE_URL}/markets/{ticker}"
    resp = requests.get(url)
    if resp.status_code != 200:
        return None
    market = resp.json().get("market", {})
    event_time = market.get("expected_expiration_time") or market.get("close_time")
    if event_time:
        return datetime.datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    return None


def get_label(ticker: str) -> str:
    """Extract short label from ticker (last part after -)."""
    return ticker.split("-")[-1] if "-" in ticker else ticker[:6]


# =============================================================================
# MARKET MAKER
# =============================================================================

def adaptive_market_maker(
    client: KalshiClient,
    ticker_a: str,
    ticker_b: str,
    theo_a: float,
    theo_b: float,
    contracts: int = 10,
    edge_min: float = 1.0,
    check_interval: float = 2.0,
    event_time: Optional[datetime.datetime] = None,
    retest_interval: int = 300,
    inventory_max: int = 50,
):
    """
    Adaptive market maker for any binary Kalshi market.

    Args:
        client: Authenticated KalshiClient
        ticker_a: First outcome ticker
        ticker_b: Second outcome ticker
        theo_a: Theo probability for A in cents
        theo_b: Theo probability for B in cents
        contracts: Contracts per order
        edge_min: Minimum edge from theo (cents)
        check_interval: Seconds between checks
        event_time: When to cancel all orders
        retest_interval: Seconds between retests
        inventory_max: Max inventory in either direction
    """
    label_a = get_label(ticker_a)
    label_b = get_label(ticker_b)

    print("=" * 60)
    print(f"ADAPTIVE MARKET MAKER: {label_a} vs {label_b}")
    print("=" * 60)

    if event_time is None:
        event_time = get_event_start_time(ticker_a)
        if event_time is None:
            event_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)

    print(f"Event time: {event_time.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Edge minimum: {edge_min}c from theo")
    print(f"Check interval: {check_interval}s")
    print(f"Retest interval: {retest_interval}s")
    print(f"Inventory max: {inventory_max} (+ = long {label_a}, - = long {label_b})")

    print(f"\nTheo: {label_a} {theo_a}c, {label_b} {theo_b}c")
    print(f"{label_a} bid ceiling: {int(theo_a - edge_min)}c")
    print(f"{label_b} bid ceiling: {int(theo_b - edge_min)}c")

    # Track orders
    our_orders: dict[tuple[str, str], OrderState] = {}

    # Initialize inventory from positions
    inventory = 0
    print("\nFetching current positions...")
    for ticker in [ticker_a, ticker_b]:
        positions = client.get_positions(ticker=ticker)
        for pos in positions:
            if pos.get("ticker") != ticker:
                continue
            yes_count = pos.get("position", 0)
            if ticker == ticker_a:
                inventory += yes_count
            else:
                inventory -= yes_count
            if yes_count != 0:
                label = get_label(ticker)
                print(f"  {label}: {yes_count} contracts")
    print(f"  Starting inventory: {inventory}")

    expiration_ts = int(event_time.timestamp())

    def place_or_update(ticker: str, side: str, is_yes: bool, target_price: int):
        key = (ticker, side)
        current = our_orders.get(key)
        label = get_label(ticker)

        if target_price == -1 or target_price == -2:
            if current:
                try:
                    client.cancel_order(current.order_id)
                    if target_price == -1:
                        print(f"  BACKING OFF {label} {side.upper()} - competitor above ceiling")
                    else:
                        print(f"  INVENTORY LIMIT {label} {side.upper()} - cancelling (inv: {inventory})")
                    del our_orders[key]
                except:
                    pass
            return

        if current and current.price == target_price:
            return

        if current:
            try:
                client.cancel_order(current.order_id)
                print(f"  Cancelled {label} {side.upper()} @ {current.price}c")
            except:
                pass

        result = client.place_order(
            ticker=ticker,
            side="buy",
            is_yes=is_yes,
            price_cents=target_price,
            count=contracts,
            expiration_ts=expiration_ts
        )

        order_id = result.get("order", {}).get("order_id")
        if order_id:
            our_orders[key] = OrderState(
                order_id=order_id,
                ticker=ticker,
                side=side,
                price=target_price,
                count=contracts
            )
            print(f"  Placed {label} {side.upper()} @ {target_price}c")
        else:
            print(f"  ERROR placing {label} {side.upper()}: {result}")

    last_retest_time = datetime.datetime.now(datetime.timezone.utc)
    RETEST_INTERVAL = retest_interval

    def update_quotes() -> bool:
        nonlocal last_retest_time, inventory

        now = datetime.datetime.now(datetime.timezone.utc)
        is_retest = (now - last_retest_time).total_seconds() >= RETEST_INTERVAL
        if is_retest:
            print(f"\n[{now.strftime('%H:%M:%S')}] RETEST | Inventory: {inventory}")
            last_retest_time = now

        # Inventory limits - determine which sides we can quote
        # inventory >= max: only quote B YES, A NO (reduces inventory)
        # inventory <= -max: only quote A YES, B NO (reduces inventory)
        can_quote_a_yes = inventory < inventory_max
        can_quote_b_no = inventory < inventory_max
        can_quote_b_yes = inventory > -inventory_max
        can_quote_a_no = inventory > -inventory_max

        # Must-quote mode: when at limit, ALWAYS quote reducing sides at ceiling
        at_positive_limit = inventory >= inventory_max   # long A, need to reduce
        at_negative_limit = inventory <= -inventory_max  # long B, need to reduce
        must_quote_b_yes = at_positive_limit  # B YES reduces positive inventory
        must_quote_a_no = at_positive_limit   # A NO reduces positive inventory
        must_quote_a_yes = at_negative_limit  # A YES reduces negative inventory
        must_quote_b_no = at_negative_limit   # B NO reduces negative inventory

        book_a = get_book_with_depth(ticker_a)
        book_b = get_book_with_depth(ticker_b)

        a_yes_current = our_orders.get((ticker_a, "yes"))
        b_yes_current = our_orders.get((ticker_b, "yes"))
        a_no_current = our_orders.get((ticker_a, "no"))
        b_no_current = our_orders.get((ticker_b, "no"))

        # Calculate prices
        if can_quote_a_yes:
            a_yes_price = calculate_adaptive_price(
                theo_a, book_a["best_bid"], book_a["second_bid"], "bid", edge_min,
                a_yes_current.price if a_yes_current else None,
                sticky_ceiling=True, is_retest=is_retest,
                best_qty=book_a["best_bid_qty"], our_size=contracts,
                must_quote=must_quote_a_yes)
        else:
            a_yes_price = -2

        if can_quote_b_yes:
            b_yes_price = calculate_adaptive_price(
                theo_b, book_b["best_bid"], book_b["second_bid"], "bid", edge_min,
                b_yes_current.price if b_yes_current else None,
                sticky_ceiling=True, is_retest=is_retest,
                best_qty=book_b["best_bid_qty"], our_size=contracts,
                must_quote=must_quote_b_yes)
        else:
            b_yes_price = -2

        if can_quote_a_no:
            a_no_price = calculate_adaptive_price(
                theo_b, book_a["best_no_bid"], book_a["second_no_bid"], "bid", edge_min,
                a_no_current.price if a_no_current else None,
                sticky_ceiling=True, is_retest=is_retest,
                best_qty=book_a["best_no_bid_qty"], our_size=contracts,
                must_quote=must_quote_a_no)
        else:
            a_no_price = -2

        if can_quote_b_no:
            b_no_price = calculate_adaptive_price(
                theo_a, book_b["best_no_bid"], book_b["second_no_bid"], "bid", edge_min,
                b_no_current.price if b_no_current else None,
                sticky_ceiling=True, is_retest=is_retest,
                best_qty=book_b["best_no_bid_qty"], our_size=contracts,
                must_quote=must_quote_b_no)
        else:
            b_no_price = -2

        current_prices = {
            (ticker_a, "yes"): a_yes_price,
            (ticker_b, "yes"): b_yes_price,
            (ticker_a, "no"): a_no_price,
            (ticker_b, "no"): b_no_price,
        }

        changes_needed = []
        for key, target_price in current_prices.items():
            current = our_orders.get(key)
            if not current or current.price != target_price:
                changes_needed.append((key, target_price))

        if not changes_needed:
            return False

        now = datetime.datetime.now(datetime.timezone.utc)
        rebalance_msg = ""
        if at_positive_limit:
            rebalance_msg = f" | REBALANCING (inv: +{inventory}, quoting {label_b} YES/{label_a} NO)"
        elif at_negative_limit:
            rebalance_msg = f" | REBALANCING (inv: {inventory}, quoting {label_a} YES/{label_b} NO)"
        print(f"\n[{now.strftime('%H:%M:%S')}] Book changed - re-quoting{rebalance_msg}")
        print(f"  {label_a}: {book_a['best_bid']}c / {book_a['best_ask']}c | {label_b}: {book_b['best_bid']}c / {book_b['best_ask']}c")

        place_or_update(ticker_a, "yes", True, a_yes_price)
        place_or_update(ticker_b, "yes", True, b_yes_price)
        place_or_update(ticker_a, "no", False, a_no_price)
        place_or_update(ticker_b, "no", False, b_no_price)

        return True

    # Cancel existing orders
    print("\nCancelling any existing orders...")
    cancelled = client.cancel_all_orders(ticker=ticker_a)
    cancelled += client.cancel_all_orders(ticker=ticker_b)
    if cancelled > 0:
        print(f"  Cancelled {cancelled} existing orders")

    print("\nStarting adaptive loop... (Ctrl+C to stop)")
    print("-" * 60)

    update_quotes()

    try:
        last_status_time = datetime.datetime.now(datetime.timezone.utc)
        while True:
            now = datetime.datetime.now(datetime.timezone.utc)

            if now >= event_time:
                print(f"\n[{now.strftime('%H:%M:%S')}] EVENT STARTING - Cancelling all orders!")
                for order in our_orders.values():
                    try:
                        client.cancel_order(order.order_id)
                    except:
                        pass
                print("Exiting.")
                break

            # Check for fills
            fills_detected = False
            for key, order in list(our_orders.items()):
                try:
                    resp = client.get(f"/portfolio/orders/{order.order_id}")
                    order_data = resp.json().get("order", {})
                    fill_count = order_data.get("fill_count", 0)
                    remaining_count = order_data.get("remaining_count", order.count)

                    new_fills = fill_count - order.filled
                    if new_fills > 0:
                        ticker, side = key
                        label = get_label(ticker)
                        # A YES / B NO → + inventory
                        # B YES / A NO → - inventory
                        if (ticker == ticker_a and side == "yes") or (ticker == ticker_b and side == "no"):
                            inventory += new_fills
                        else:
                            inventory -= new_fills

                        if remaining_count == 0:
                            print(f"\n[{now.strftime('%H:%M:%S')}] FILLED: {label} {side.upper()} {fill_count}/{order.count} @ {order.price}c | Inventory: {inventory}")
                        else:
                            print(f"\n[{now.strftime('%H:%M:%S')}] PARTIAL: {label} {side.upper()} {new_fills} filled @ {order.price}c ({remaining_count} cancelled) | Inventory: {inventory}")
                            try:
                                client.cancel_order(order.order_id)
                            except:
                                pass

                        del our_orders[key]
                        fills_detected = True
                except:
                    pass

            changed = update_quotes()

            if not changed and not fills_detected:
                if (now - last_status_time).seconds >= 60:
                    time_to_event = event_time - now
                    our_prices = [f"{get_label(k[0])} {k[1].upper()}@{v.price}c" for k, v in our_orders.items()]
                    print(f"[{now.strftime('%H:%M:%S')}] Watching... {str(time_to_event).split('.')[0]} to event | Inv: {inventory} | {', '.join(our_prices)}")
                    last_status_time = now

            time.sleep(check_interval)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        cancel = input("Cancel all orders? (y/n): ").strip().lower()
        if cancel == 'y':
            for order in our_orders.values():
                try:
                    client.cancel_order(order.order_id)
                except:
                    pass
            print(f"Cancelled {len(our_orders)} orders.")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generalized Kalshi Market Maker")
    parser.add_argument("--ticker-a", required=True, help="First outcome ticker")
    parser.add_argument("--ticker-b", required=True, help="Second outcome ticker")
    parser.add_argument("--odds-a", type=float, required=True, help="Decimal odds for outcome A")
    parser.add_argument("--odds-b", type=float, required=True, help="Decimal odds for outcome B")
    parser.add_argument("--contracts", type=int, default=10, help="Contracts per order (default: 10)")
    parser.add_argument("--edge", type=float, default=1.0, help="Min edge from theo in cents (default: 1.0)")
    parser.add_argument("--interval", type=float, default=2.0, help="Check interval in seconds (default: 2.0)")
    parser.add_argument("--retest", type=int, default=300, help="Retest interval in seconds (default: 300)")
    parser.add_argument("--inventory-max", type=int, default=50, help="Max inventory exposure (default: 50)")
    parser.add_argument("--dry-run", action="store_true", help="Show theo calculation without trading")

    args = parser.parse_args()

    # Calculate theo
    theo = calculate_theo(args.odds_a, args.odds_b)
    label_a = get_label(args.ticker_a)
    label_b = get_label(args.ticker_b)

    print(f"\nOdds: {label_a} {args.odds_a}, {label_b} {args.odds_b}")
    print(f"Theo: {label_a} {theo['a']}c, {label_b} {theo['b']}c (vig: {theo['vig']}%)")

    if args.dry_run:
        print("\n[DRY RUN - No orders placed]")
        exit(0)

    # Load credentials
    try:
        from config.config import KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH
        client = KalshiClient(KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH)
        balance = client.get_balance()
        print(f"Authenticated. Balance: ${balance.get('balance', 0) / 100:.2f}")
    except ImportError:
        print("ERROR: Add KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH to config/config.py")
        exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        exit(1)

    adaptive_market_maker(
        client=client,
        ticker_a=args.ticker_a,
        ticker_b=args.ticker_b,
        theo_a=theo["a"],
        theo_b=theo["b"],
        contracts=args.contracts,
        edge_min=args.edge,
        check_interval=args.interval,
        retest_interval=args.retest,
        inventory_max=args.inventory_max
    )
