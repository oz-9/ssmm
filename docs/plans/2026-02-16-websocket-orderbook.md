# WebSocket Orderbook Integration Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace REST polling with Kalshi WebSocket for real-time orderbook updates, enabling immediate reaction to market changes.

**Architecture:** Single persistent WebSocket connection subscribes to `orderbook_delta` channel for all active market tickers. Local orderbook state maintained per ticker, updated via deltas. Trading logic triggers immediately on orderbook changes instead of polling. REST API retained for order placement/cancellation.

**Tech Stack:** Python asyncio, `websockets` library, FastAPI (existing)

---

## Background

**Current State (polling):**
- `trading_loop()` polls every 2s via REST `GET /markets/{ticker}/orderbook`
- Timing-based mechanisms: `sticky_reset_secs` (10s), `overbid_cancel_delay` (10s)
- 2 REST calls per active match per loop iteration

**Target State (WebSocket):**
- Single WebSocket connection to `wss://api.elections.kalshi.com/trade-api/ws/v2`
- Subscribe to `orderbook_delta` for each active market
- React immediately when orderbook changes
- Keep ceiling reset and overbid delay timers (time-based, not poll-based)

**Kalshi WebSocket Protocol:**
- Auth via RSA-PSS signed headers during handshake
- Subscribe: `{"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": ["TICKER"]}}`
- Receive `orderbook_snapshot` first, then `orderbook_delta` messages
- Delta format: `{type, market_ticker, price, delta, side}` where delta is quantity change at price level

---

## Task 1: Add websockets dependency

**Files:**
- Modify: `requirements.txt` (or create if doesn't exist)

**Step 1: Check if requirements.txt exists**

Run: `dir requirements.txt`

**Step 2: Add websockets to dependencies**

If exists, append. If not, create:
```
websockets>=12.0
```

**Step 3: Install**

Run: `pip install websockets`

**Step 4: Commit**

```bash
git add requirements.txt
git commit -m "deps: add websockets library"
```

---

## Task 2: Create KalshiWebSocket class in mm.py

**Files:**
- Modify: `mm.py` (add after KalshiClient class, around line 120)

**Step 1: Add imports at top of mm.py**

```python
import asyncio
import websockets
import json
```

**Step 2: Add KalshiWebSocket class**

```python
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
```

**Step 3: Verify syntax**

Run: `python -m py_compile mm.py`
Expected: No output (success)

**Step 4: Commit**

```bash
git add mm.py
git commit -m "feat: add KalshiWebSocket class for orderbook streaming"
```

---

## Task 3: Refactor dashboard.py to use WebSocket for orderbooks

**Files:**
- Modify: `dashboard.py`

**Step 1: Update imports**

Add at top after existing imports:
```python
from mm import KalshiWebSocket
```

**Step 2: Add WebSocket client to global state (around line 179)**

Replace:
```python
client: Optional[KalshiClient] = None
```

With:
```python
client: Optional[KalshiClient] = None
ws_client: Optional["KalshiWebSocket"] = None
```

**Step 3: Update init_client() to also create WebSocket client**

Replace the `init_client()` function:
```python
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
```

**Step 4: Add orderbook change handler**

Add new function after `refresh_orderbooks()`:
```python
async def on_orderbook_change(ticker: str, book: dict):
    """Handle orderbook update from WebSocket."""
    # Find which match this ticker belongs to
    for match in matches.values():
        if not match.active:
            continue

        if ticker == match.market_a.ticker:
            match.market_a.best_bid = book["best_bid"]
            match.market_a.best_ask = book["best_ask"]
            # Trigger quote update for this match
            await handle_match_update(match)
            break
        elif ticker == match.market_b.ticker:
            match.market_b.best_bid = book["best_bid"]
            match.market_b.best_ask = book["best_ask"]
            await handle_match_update(match)
            break
```

**Step 5: Add handle_match_update function**

Add after `on_orderbook_change`:
```python
async def handle_match_update(match: Match):
    """Process a match when its orderbook changes."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)

    # Check event time
    if match.event_time and now >= match.event_time:
        print(f"[{match.id}] Event started - stopping")
        match.active = False
        await cancel_match_orders(match)
        return

    # Get books from WebSocket cache
    book_a = ws_client.get_book(match.market_a.ticker) if ws_client else get_book_with_depth(match.market_a.ticker)
    book_b = ws_client.get_book(match.market_b.ticker) if ws_client else get_book_with_depth(match.market_b.ticker)

    # Update quotes
    await update_quotes(match, book_a, book_b)

    # Broadcast state to dashboard clients
    await broadcast(get_state())
```

**Step 6: Replace trading_loop with WebSocket-driven loop**

Replace the entire `trading_loop()` function:
```python
async def trading_loop():
    """Main trading loop - WebSocket driven with periodic tasks."""
    global orders

    print("Trading loop started (WebSocket mode)")

    # Connect WebSocket and register callback
    if ws_client:
        ws_client.on_orderbook_change(on_orderbook_change)
        await ws_client.connect()

    # Start WebSocket listener in background
    ws_task = asyncio.create_task(ws_client.listen()) if ws_client else None

    # Periodic tasks (inventory sync, fill checks, sticky reset)
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

                # Sync inventory from Kalshi positions (less frequent)
                match.inventory = calculate_match_inventory(match)

                # Check for fills
                await check_fills(match)

            # Broadcast state periodically
            await broadcast(get_state())

        except Exception as e:
            print(f"Trading loop error: {e}")

        # Periodic check interval (for fills/inventory, not orderbook)
        await asyncio.sleep(settings.check_interval)
```

**Step 7: Update match activation to subscribe/unsubscribe**

Modify `api_start_match`:
```python
@app.post("/api/matches/{match_id}/start")
async def api_start_match(match_id: str):
    if match_id in matches:
        match = matches[match_id]
        match.active = True
        # Subscribe to orderbook WebSocket
        if ws_client:
            await ws_client.subscribe([match.market_a.ticker, match.market_b.ticker])
        await broadcast(get_state())
    return {"ok": True}
```

Modify `api_stop_match`:
```python
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
```

**Step 8: Verify syntax**

Run: `python -m py_compile dashboard.py`
Expected: No output (success)

**Step 9: Commit**

```bash
git add dashboard.py
git commit -m "feat: switch dashboard to WebSocket-driven orderbook updates"
```

---

## Task 4: Add reconnection with exponential backoff

**Files:**
- Modify: `mm.py` (KalshiWebSocket.reconnect method)

**Step 1: Update reconnect method with exponential backoff**

Replace the `reconnect` method in KalshiWebSocket:
```python
async def reconnect(self):
    """Reconnect with exponential backoff."""
    backoff = 1
    max_backoff = 60

    while True:
        try:
            print(f"Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)

            tickers = list(self.subscribed_tickers)
            self.subscribed_tickers.clear()
            self.orderbooks.clear()

            await self.connect()
            if tickers:
                await self.subscribe(tickers)

            print("Reconnected successfully")
            return
        except Exception as e:
            print(f"Reconnection failed: {e}")
            backoff = min(backoff * 2, max_backoff)
```

**Step 2: Wrap listen() to auto-reconnect**

Update the `listen` method:
```python
async def listen(self):
    """Main loop to receive and process messages with auto-reconnect."""
    while True:
        if not self.ws:
            await self.connect()

        try:
            async for message in self.ws:
                try:
                    msg = json.loads(message)
                    await self._handle_message(msg)
                except json.JSONDecodeError:
                    pass
        except websockets.ConnectionClosed as e:
            print(f"WebSocket disconnected ({e.code}), reconnecting...")
            self.ws = None
            await self.reconnect()
        except Exception as e:
            print(f"WebSocket error: {e}, reconnecting...")
            self.ws = None
            await self.reconnect()
```

**Step 3: Commit**

```bash
git add mm.py
git commit -m "feat: add exponential backoff reconnection to WebSocket"
```

---

## Task 5: Test the integration

**Step 1: Run the dashboard**

Run: `python dashboard.py`

Expected output:
```
Kalshi authenticated. Balance: $XX.XX
Trading loop started (WebSocket mode)
Kalshi WebSocket connected
```

**Step 2: Add a match via UI**

Open http://localhost:8000, add a match with valid tickers.

**Step 3: Start the match**

Click Start. Expected console output:
```
Subscribed to orderbook: ['TICKER_A', 'TICKER_B']
```

**Step 4: Verify real-time updates**

Watch console for orderbook changes triggering quote updates without polling delays.

**Step 5: Stop the match**

Click Stop. Expected:
```
Unsubscribed from orderbook: ['TICKER_A', 'TICKER_B']
```

---

## Task 6: Remove unused polling code

**Files:**
- Modify: `dashboard.py`

**Step 1: Remove refresh_orderbooks function**

Delete the `refresh_orderbooks()` function (lines 300-311) - no longer used.

**Step 2: Remove track_request calls for orderbook**

The `track_request()` calls in the old trading loop for orderbook fetches can be removed since we're not polling anymore. Keep track_request for REST API order operations.

**Step 3: Update rate tracking to only count REST calls**

Rate tracking now only relevant for order placement/cancellation, not orderbook polling.

**Step 4: Commit**

```bash
git add dashboard.py
git commit -m "refactor: remove unused polling code after WebSocket migration"
```

---

## Verification

1. **Start dashboard:** `python dashboard.py`
2. **Add match** with real tickers (e.g., from Valorant or CS2)
3. **Start trading** and watch for:
   - "Kalshi WebSocket connected" on startup
   - "Subscribed to orderbook" when match starts
   - Immediate quote adjustments when orderbook changes (no 2s delay)
4. **Test reconnection:** Kill network briefly, verify "Reconnecting..." and recovery
5. **Stop match:** Verify unsubscribe and order cancellation
6. **Kill all:** Verify `/api/kill` still works with WebSocket active

---

## Rollback

If issues arise, the REST polling approach can be restored by:
1. Reverting `trading_loop()` to poll-based version
2. Not initializing `ws_client`
3. Using `get_book_with_depth()` directly in `update_quotes()`
