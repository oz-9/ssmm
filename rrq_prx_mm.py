"""
Market Maker for RRQ vs Paper Rex Valorant match.
Focused single-market implementation.
"""

import requests
import base64
import datetime
import time
from dataclasses import dataclass
from typing import Optional
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# =============================================================================
# CONFIG
# =============================================================================

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# The two markets for this match
PRX_TICKER = "KXVALORANTGAME-26FEB15RRQPR-PR"   # Paper Rex YES
RRQ_TICKER = "KXVALORANTGAME-26FEB15RRQPR-RRQ"  # RRQ YES

# Sportsbook odds - NO-VIG FAIR ODDS
# Preferred books for Valorant: GGBet, Thunderpick, Rainbet
# Raw: Thunderpick 1.46/2.57, Rainbet 1.45/2.60, GGBet 1.46/2.57
# No-vig theo: PRX 63.9%, RRQ 36.1%
ODDS = {
    "PRX": {"decimal": 1.46, "source": "avg(ggbet,thunderpick,rainbet)"},
    "RRQ": {"decimal": 2.57, "source": "avg(ggbet,thunderpick,rainbet)"},
}

# OddsPapi config for live odds refresh
ODDSPAPI_BASE = "https://api.oddspapi.io/v4"
VALORANT_SPORT_ID = 61
TEAM_ALIASES = {
    "PRX": ["paper rex", "prx", "paper"],
    "RRQ": ["rrq", "rex regum qeon", "rex regum"],
}


def get_oddspapi_key() -> Optional[str]:
    """Get OddsPapi API key from config or environment."""
    try:
        from config.config import ODDSPAPI_API_KEY
        return ODDSPAPI_API_KEY
    except (ImportError, AttributeError):
        import os
        return os.environ.get("ODDSPAPI_API_KEY")


def fetch_live_odds() -> Optional[dict]:
    """
    Fetch live odds for RRQ vs PRX from OddsPapi.
    Returns dict with PRX and RRQ decimal odds, or None if unavailable.
    """
    api_key = get_oddspapi_key()
    if not api_key:
        return None

    try:
        url = f"{ODDSPAPI_BASE}/fixtures"
        params = {
            "apiKey": api_key,
            "sportId": VALORANT_SPORT_ID,
            "hasOdds": "true"
        }
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            print(f"  OddsPapi error: {resp.status_code}")
            return None

        fixtures = resp.json().get("data", [])

        # Find RRQ vs PRX match
        for fixture in fixtures:
            home = fixture.get("homeTeam", {}).get("name", "").lower()
            away = fixture.get("awayTeam", {}).get("name", "").lower()

            # Check if this is our match
            is_prx = any(alias in home or alias in away for alias in TEAM_ALIASES["PRX"])
            is_rrq = any(alias in home or alias in away for alias in TEAM_ALIASES["RRQ"])

            if is_prx and is_rrq:
                odds_list = fixture.get("odds", [])
                if not odds_list:
                    continue

                # Get best odds (prefer pinnacle)
                prx_odds = None
                rrq_odds = None
                best_source = "unknown"

                for book in odds_list:
                    book_name = book.get("bookmaker", "").lower()
                    outcomes = book.get("outcomes", [])

                    for outcome in outcomes:
                        name = outcome.get("name", "").lower()
                        odds_val = outcome.get("odds")

                        if odds_val:
                            if any(alias in name for alias in TEAM_ALIASES["PRX"]):
                                if prx_odds is None or "pinnacle" in book_name:
                                    prx_odds = float(odds_val)
                                    best_source = book_name
                            elif any(alias in name for alias in TEAM_ALIASES["RRQ"]):
                                if rrq_odds is None or "pinnacle" in book_name:
                                    rrq_odds = float(odds_val)

                if prx_odds and rrq_odds:
                    return {"PRX": prx_odds, "RRQ": rrq_odds, "source": best_source}

        return None
    except Exception as e:
        print(f"  OddsPapi fetch error: {e}")
        return None


def refresh_odds() -> bool:
    """
    Refresh odds from OddsPapi. Updates global ODDS dict.
    Returns True if odds were updated.
    """
    global ODDS
    live = fetch_live_odds()

    if live:
        old_prx = ODDS["PRX"]["decimal"]
        old_rrq = ODDS["RRQ"]["decimal"]

        ODDS["PRX"]["decimal"] = live["PRX"]
        ODDS["RRQ"]["decimal"] = live["RRQ"]
        ODDS["PRX"]["source"] = live["source"]
        ODDS["RRQ"]["source"] = live["source"]

        if live["PRX"] != old_prx or live["RRQ"] != old_rrq:
            print(f"  Odds updated: PRX {old_prx} -> {live['PRX']}, RRQ {old_rrq} -> {live['RRQ']}")
            return True
        return False
    return False

# =============================================================================
# KALSHI AUTH
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
        # Full path including /trade-api/v2 prefix, without query params
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

    # --- Account ---
    def get_balance(self) -> dict:
        resp = self.get("/portfolio/balance")
        return resp.json()

    # --- Market Data ---
    def get_market(self, ticker: str) -> dict:
        resp = requests.get(f"{self.base_url}/markets/{ticker}")
        return resp.json().get("market", {})

    def get_orderbook(self, ticker: str) -> dict:
        resp = requests.get(f"{self.base_url}/markets/{ticker}/orderbook")
        return resp.json().get("orderbook", {})

    # --- Orders ---
    def place_order(self, ticker: str, side: str, is_yes: bool, price_cents: int, count: int,
                    expiration_ts: Optional[int] = None) -> dict:
        """
        Place a limit order.
        side: "buy" or "sell"
        is_yes: True for YES contracts, False for NO
        price_cents: 1-99
        count: number of contracts
        expiration_ts: Unix timestamp in seconds when order expires (None = GTC)
        """
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
        """Cancel all resting orders, optionally filtered by ticker."""
        orders = self.get_orders(ticker=ticker, status="resting")
        cancelled = 0
        for order in orders:
            self.cancel_order(order["order_id"])
            cancelled += 1
        return cancelled

    def get_positions(self, ticker: str = None) -> list:
        """Get current positions, optionally filtered by ticker."""
        path = "/portfolio/positions"
        if ticker:
            path += f"?ticker={ticker}"
        resp = self.get(path)
        return resp.json().get("market_positions", [])


# =============================================================================
# THEO CALCULATION
# =============================================================================

def decimal_to_prob(odds: float) -> float:
    """Convert decimal odds to implied probability."""
    return 1 / odds if odds > 0 else 0


def calculate_theo() -> dict:
    """Calculate theo probabilities from sportsbook odds."""
    prx_implied = decimal_to_prob(ODDS["PRX"]["decimal"])
    rrq_implied = decimal_to_prob(ODDS["RRQ"]["decimal"])

    total = prx_implied + rrq_implied  # Should be > 1 (vig)

    # Remove vig proportionally
    prx_theo = prx_implied / total
    rrq_theo = rrq_implied / total

    return {
        "PRX": round(prx_theo * 100, 1),  # in cents
        "RRQ": round(rrq_theo * 100, 1),
        "vig": round((total - 1) * 100, 2),
        "source": ODDS["PRX"]["source"]
    }


# =============================================================================
# ORDERBOOK ANALYSIS
# =============================================================================

@dataclass
class MarketState:
    ticker: str
    team: str
    best_bid: int
    best_ask: int
    bid_depth: int
    ask_depth: int

    @property
    def spread(self) -> int:
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2


def get_market_state(ticker: str, team: str) -> MarketState:
    """Get current market state from orderbook."""
    url = f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook"
    resp = requests.get(url)
    orderbook = resp.json().get("orderbook", {})

    yes_bids = orderbook.get("yes") or []
    no_bids = orderbook.get("no") or []

    best_yes_bid = max([b[0] for b in yes_bids], default=0)
    best_no_bid = max([b[0] for b in no_bids], default=0)

    best_yes_ask = 100 - best_no_bid if best_no_bid > 0 else 100

    yes_depth = sum(b[1] for b in yes_bids)
    no_depth = sum(b[1] for b in no_bids)

    return MarketState(
        ticker=ticker,
        team=team,
        best_bid=best_yes_bid,
        best_ask=best_yes_ask,
        bid_depth=yes_depth,
        ask_depth=no_depth
    )


# =============================================================================
# MARKET MAKING LOGIC
# =============================================================================

def calculate_mm_prices(theo: float, market: MarketState, edge_target: float = 2.0) -> dict:
    """
    Calculate market making bid/ask prices.

    theo: theo probability in cents (e.g., 70.5)
    edge_target: minimum edge we want per side in cents

    Returns dict with our_bid, our_ask prices
    """
    # Our bid should be below theo by at least edge_target
    our_bid = min(int(theo - edge_target), market.best_bid + 1)
    our_bid = max(1, min(99, our_bid))  # Clamp to valid range

    # Our ask should be above theo by at least edge_target
    our_ask = max(int(theo + edge_target), market.best_ask - 1)
    our_ask = max(1, min(99, our_ask))

    return {
        "our_bid": our_bid,
        "our_ask": our_ask,
        "theo": theo,
        "edge_if_bid_fills": round(theo - our_bid, 1),
        "edge_if_ask_fills": round(our_ask - theo, 1),
    }


def calculate_adaptive_price(theo: float, best_price: int, second_price: int, side: str, edge_min: float = 1.0, our_current: Optional[int] = None, sticky_ceiling: bool = False, is_retest: bool = False, best_qty: int = 0, our_size: int = 0, must_quote: bool = False) -> int:
    """
    Calculate adaptive price to stay 1c above competition within theo ceiling.

    Args:
        theo: theo probability in cents (e.g., 70.5)
        best_price: current best bid in the book (may include our order)
        second_price: second best bid (the competition if we're at best)
        side: "bid" or "ask"
        edge_min: minimum edge from theo (default 1c)
        our_current: our current price (to identify our order in book)
        sticky_ceiling: if True, stay at ceiling even if competition drops
        is_retest: if True, drop down to find better price (overrides sticky)
        best_qty: total quantity at best price level
        our_size: our order size (to detect ties via quantity)
        must_quote: if True, always quote at ceiling even if not competitive (for inventory rebalancing)

    Returns:
        Our target price (clamped to valid range)
    """
    if side == "bid":
        ceiling = int(theo - edge_min)

        if our_current is not None and best_price == our_current:
            # We're at top of book
            # Tied if qty at best price > our size (others are there too)
            tied_at_top = best_qty > our_size

            if tied_at_top and our_current < ceiling:
                # Competition matched us - go up 1c to have clear priority
                target = our_current + 1
            elif sticky_ceiling and not is_retest:
                # Ahead of competition, stay sticky
                target = our_current
            else:
                # Retest mode: drop to 1c above competition
                target = second_price + 1 if second_price > 0 else 1
        elif our_current is not None and best_price > our_current:
            # Someone outbid us
            if best_price > ceiling:
                # They went ABOVE ceiling - overpaying
                if must_quote:
                    # Inventory rebalancing: stay at ceiling, wait for price to come back
                    return max(1, ceiling)
                return -1
            # Beat them (up to ceiling)
            target = best_price + 1
        else:
            # No current order - bid 1 above best
            if best_price > ceiling:
                # Book is above ceiling
                if must_quote:
                    # Inventory rebalancing: quote at ceiling anyway
                    return max(1, ceiling)
                return -1
            target = best_price + 1

        return max(1, min(ceiling, target))
    else:  # ask
        floor = int(theo + edge_min) + 1

        if our_current is not None and best_price == our_current:
            # We're at top of book (best ask)
            # Tied if qty at best price > our size
            tied_at_top = best_qty > our_size

            if tied_at_top and our_current > floor:
                # Competition matched us - go down 1c to have clear priority
                target = our_current - 1
            elif sticky_ceiling and not is_retest:
                # Ahead of competition, stay sticky
                target = our_current
            else:
                # Retest mode: raise to 1c below competition
                target = second_price - 1 if second_price < 100 else 99
        elif our_current is not None and best_price < our_current:
            if best_price < floor:
                # They went BELOW floor - overpaying
                if must_quote:
                    # Inventory rebalancing: stay at floor, wait for price to come back
                    return min(99, floor)
                return -1
            target = best_price - 1
        else:
            # No current order
            if best_price < floor:
                # Book is below floor
                if must_quote:
                    # Inventory rebalancing: quote at floor anyway
                    return min(99, floor)
                return -1
            target = best_price - 1

        return max(floor, min(99, target))


# =============================================================================
# MAIN MARKET MAKER
# =============================================================================

def run_market_maker(client: KalshiClient = None, contracts: int = 10, dry_run: bool = True, four_sided: bool = False, event_time_override: Optional[datetime.datetime] = None):
    """
    Run the market maker for RRQ vs PRX match.

    Args:
        client: Authenticated KalshiClient (None for dry run)
        contracts: Number of contracts per order
        dry_run: If True, just print what would happen
        four_sided: If True, quote YES and NO on both markets (LOOP arbitrage)
    """
    print("=" * 60)
    print("RRQ vs PAPER REX MARKET MAKER")
    print("=" * 60)

    # 1. Calculate theo
    theo = calculate_theo()
    print(f"\nTHEO (from {theo['source']}, vig: {theo['vig']}%):")
    print(f"  Paper Rex: {theo['PRX']}¢")
    print(f"  RRQ:       {theo['RRQ']}¢")

    # 2. Get current market state
    prx_market = get_market_state(PRX_TICKER, "PRX")
    rrq_market = get_market_state(RRQ_TICKER, "RRQ")

    print(f"\nCURRENT ORDERBOOK:")
    print(f"  PRX: {prx_market.best_bid}¢ / {prx_market.best_ask}¢ (spread: {prx_market.spread}¢)")
    print(f"  RRQ: {rrq_market.best_bid}¢ / {rrq_market.best_ask}¢ (spread: {rrq_market.spread}¢)")

    # 3. Calculate our MM prices
    prx_prices = calculate_mm_prices(theo["PRX"], prx_market)
    rrq_prices = calculate_mm_prices(theo["RRQ"], rrq_market)

    if four_sided:
        print(f"\nFOUR-SIDED STRATEGY (LOOP Arbitrage):")
        print(f"  PRX YES bid: {prx_prices['our_bid']}¢ (theo: {theo['PRX']}¢)")
        print(f"  RRQ NO  bid: {prx_prices['our_bid']}¢ (same exposure as PRX YES)")
        print(f"  RRQ YES bid: {rrq_prices['our_bid']}¢ (theo: {theo['RRQ']}¢)")
        print(f"  PRX NO  bid: {rrq_prices['our_bid']}¢ (same exposure as RRQ YES)")

        # If all 4 fill, we have 2 units of the arb
        total_cost = 2 * (prx_prices['our_bid'] + rrq_prices['our_bid'])
        profit_if_all_fill = 200 - total_cost

        print(f"\nIF ALL 4 BIDS FILL:")
        print(f"  Total cost: {total_cost}¢")
        print(f"  Guaranteed payout: 200¢")
        print(f"  Profit: {profit_if_all_fill}¢ per contract set")
        print(f"  With {contracts} contracts each: {profit_if_all_fill * contracts}¢ = ${profit_if_all_fill * contracts / 100:.2f}")
    else:
        print(f"\nTWO-SIDED STRATEGY:")
        print(f"  PRX YES bid: {prx_prices['our_bid']}¢ (edge: +{prx_prices['edge_if_bid_fills']}¢)")
        print(f"  RRQ YES bid: {rrq_prices['our_bid']}¢ (edge: +{rrq_prices['edge_if_bid_fills']}¢)")

        total_cost = prx_prices['our_bid'] + rrq_prices['our_bid']
        profit_if_all_fill = 100 - total_cost

        print(f"\nIF BOTH BIDS FILL:")
        print(f"  Total cost: {total_cost}¢")
        print(f"  Guaranteed payout: 100¢")
        print(f"  Profit: {profit_if_all_fill}¢ per contract")
        print(f"  With {contracts} contracts: {profit_if_all_fill * contracts}¢ = ${profit_if_all_fill * contracts / 100:.2f}")

    if dry_run:
        print("\n[DRY RUN - No orders placed]")
        print("\nTo place real orders, call with dry_run=False and provide KalshiClient")
        return

    # 4. Get event start time for order expiration
    event_time = event_time_override or get_event_start_time(PRX_TICKER)
    # Convert to Unix timestamp (seconds) for Kalshi API
    expiration_ts = int(event_time.timestamp()) if event_time else None

    print(f"\nPLACING ORDERS...")
    if expiration_ts:
        print(f"  Orders expire at: {event_time.strftime('%Y-%m-%d %H:%M:%S UTC')} (ts: {expiration_ts})")
    else:
        print(f"  WARNING: No expiration set (GTC)")

    orders = []

    # Place PRX YES bid
    prx_yes_order = client.place_order(
        ticker=PRX_TICKER,
        side="buy",
        is_yes=True,
        price_cents=prx_prices['our_bid'],
        count=contracts,
        expiration_ts=expiration_ts
    )
    orders.append(("PRX YES", prx_yes_order))
    print(f"  PRX YES @ {prx_prices['our_bid']}¢: {prx_yes_order.get('order', {}).get('order_id', 'ERROR')}")

    # Place RRQ YES bid
    rrq_yes_order = client.place_order(
        ticker=RRQ_TICKER,
        side="buy",
        is_yes=True,
        price_cents=rrq_prices['our_bid'],
        count=contracts,
        expiration_ts=expiration_ts
    )
    orders.append(("RRQ YES", rrq_yes_order))
    print(f"  RRQ YES @ {rrq_prices['our_bid']}¢: {rrq_yes_order.get('order', {}).get('order_id', 'ERROR')}")

    if four_sided:
        # Place RRQ NO bid (same price as PRX YES - LOOP equivalent)
        rrq_no_order = client.place_order(
            ticker=RRQ_TICKER,
            side="buy",
            is_yes=False,
            price_cents=prx_prices['our_bid'],  # Same as PRX YES
            count=contracts,
            expiration_ts=expiration_ts
        )
        orders.append(("RRQ NO", rrq_no_order))
        print(f"  RRQ NO  @ {prx_prices['our_bid']}¢: {rrq_no_order.get('order', {}).get('order_id', 'ERROR')}")

        # Place PRX NO bid (same price as RRQ YES - LOOP equivalent)
        prx_no_order = client.place_order(
            ticker=PRX_TICKER,
            side="buy",
            is_yes=False,
            price_cents=rrq_prices['our_bid'],  # Same as RRQ YES
            count=contracts,
            expiration_ts=expiration_ts
        )
        orders.append(("PRX NO", prx_no_order))
        print(f"  PRX NO  @ {rrq_prices['our_bid']}¢: {prx_no_order.get('order', {}).get('order_id', 'ERROR')}")

    print(f"\n{len(orders)} orders placed.")
    return {"orders": orders, "theo": theo, "profit_per_set": profit_if_all_fill}


def update_odds(prx_decimal: float, rrq_decimal: float, source: str = "manual"):
    """Update the odds used for theo calculation."""
    global ODDS
    ODDS["PRX"]["decimal"] = prx_decimal
    ODDS["RRQ"]["decimal"] = rrq_decimal
    ODDS["PRX"]["source"] = source
    ODDS["RRQ"]["source"] = source
    print(f"Updated odds: PRX {prx_decimal}, RRQ {rrq_decimal} (source: {source})")


def get_event_start_time(ticker: str) -> Optional[datetime.datetime]:
    """Get the event start time for a market (when match begins, not settlement)."""
    url = f"{KALSHI_BASE_URL}/markets/{ticker}"
    resp = requests.get(url)
    if resp.status_code != 200:
        return None

    market = resp.json().get("market", {})
    # expected_expiration_time is when the match starts
    # close_time is when settlement happens (much later)
    event_time = market.get("expected_expiration_time") or market.get("close_time")

    if event_time:
        # Parse ISO format
        return datetime.datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    return None


@dataclass
class OrderState:
    """Track our current order state."""
    order_id: str
    ticker: str
    side: str  # "yes" or "no"
    price: int
    count: int
    filled: int = 0  # Track fills we've already accounted for


def get_book_with_depth(ticker: str) -> dict:
    """
    Get orderbook with price levels for finding competition.
    Returns dict with best_bid, second_bid, best_ask, second_ask, and quantities.
    """
    url = f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook"
    resp = requests.get(url)
    orderbook = resp.json().get("orderbook", {})

    yes_bids = orderbook.get("yes") or []  # [(price, qty), ...]
    no_bids = orderbook.get("no") or []

    # Sort bids descending (highest first)
    yes_bids_sorted = sorted(yes_bids, key=lambda x: x[0], reverse=True)
    no_bids_sorted = sorted(no_bids, key=lambda x: x[0], reverse=True)

    best_yes_bid = yes_bids_sorted[0][0] if yes_bids_sorted else 0
    best_yes_bid_qty = yes_bids_sorted[0][1] if yes_bids_sorted else 0
    second_yes_bid = yes_bids_sorted[1][0] if len(yes_bids_sorted) > 1 else 0

    best_no_bid = no_bids_sorted[0][0] if no_bids_sorted else 0
    best_no_bid_qty = no_bids_sorted[0][1] if no_bids_sorted else 0
    second_no_bid = no_bids_sorted[1][0] if len(no_bids_sorted) > 1 else 0

    # YES ask = 100 - NO bid
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


def adaptive_market_maker(
    client: KalshiClient,
    contracts: int = 10,
    edge_min: float = 1.0,
    check_interval: float = 2.0,
    event_time: Optional[datetime.datetime] = None,
    retest_interval: int = 300,
    inventory_max: int = 50,
):
    """
    Adaptive market maker that stays at top of book within theo ceiling.

    Strategy:
    - Always be best bid/ask (top of book)
    - Never bid more than (theo - edge_min) or ask less than (theo + edge_min)
    - Re-quote when outbid by competitors
    - Cancel all orders at event start
    - Track inventory: RRQ YES/PRX NO = +1, PRX YES/RRQ NO = -1
    - If |inventory| >= inventory_max, only quote reducing side

    Args:
        client: Authenticated KalshiClient
        contracts: Contracts per order
        edge_min: Minimum edge from theo in cents (default 1c)
        check_interval: Seconds between book checks
        event_time: When to cancel all orders (event start)
        inventory_max: Max exposure in either direction (default 50)
    """
    print("=" * 60)
    print("ADAPTIVE MARKET MAKER")
    print("=" * 60)

    if event_time is None:
        # Default: Feb 15, 11:00 UTC (match start)
        event_time = datetime.datetime(2026, 2, 15, 7, 0, 0, tzinfo=datetime.timezone.utc)

    print(f"Event time: {event_time.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Edge minimum: {edge_min}c from theo")
    print(f"Check interval: {check_interval}s")
    print(f"Retest interval: {retest_interval}s (sticky ceiling between retests)")
    print(f"Inventory max: {inventory_max} (+ = long RRQ, - = long PRX)")

    # Calculate theo
    theo = calculate_theo()
    prx_theo = theo["PRX"]
    rrq_theo = theo["RRQ"]

    print(f"\nTheo: PRX {prx_theo}c, RRQ {rrq_theo}c")
    print(f"PRX bid ceiling: {int(prx_theo - edge_min)}c, ask floor: {int(prx_theo + edge_min) + 1}c")
    print(f"RRQ bid ceiling: {int(rrq_theo - edge_min)}c, ask floor: {int(rrq_theo + edge_min) + 1}c")

    # Track our orders: {(ticker, side): OrderState}
    our_orders: dict[tuple[str, str], OrderState] = {}

    # Inventory tracking: + = long RRQ (want RRQ to win), - = long PRX (want PRX to win)
    # RRQ YES fill → +count, PRX NO fill → +count
    # PRX YES fill → -count, RRQ NO fill → -count
    # Initialize from current positions
    inventory = 0
    print("\nFetching current positions...")
    for ticker in [RRQ_TICKER, PRX_TICKER]:
        positions = client.get_positions(ticker=ticker)
        for pos in positions:
            if pos.get("ticker") != ticker:
                continue
            yes_count = pos.get("position", 0)  # Positive = long YES, negative = short YES
            # RRQ YES / PRX NO → + inventory
            # PRX YES / RRQ NO → - inventory
            if ticker == RRQ_TICKER:
                inventory += yes_count  # Long RRQ YES = +, Short RRQ YES (= long RRQ NO) = -
            else:  # PRX
                inventory -= yes_count  # Long PRX YES = -, Short PRX YES (= long PRX NO) = +
            if yes_count != 0:
                print(f"  {ticker[-3:]}: {yes_count} contracts")
    print(f"  Starting inventory: {inventory}")

    # Expiration timestamp
    expiration_ts = int(event_time.timestamp())

    def place_or_update(ticker: str, side: str, is_yes: bool, target_price: int):
        """Place new order or update existing if price changed. -1 means cancel only."""
        key = (ticker, side)
        current = our_orders.get(key)

        # -1 signals: competitor above ceiling, back off
        # -2 signals: inventory limit reached
        if target_price == -1 or target_price == -2:
            if current:
                try:
                    client.cancel_order(current.order_id)
                    if target_price == -1:
                        print(f"  BACKING OFF {ticker[-3:]} {side.upper()} - competitor above ceiling")
                    else:
                        print(f"  INVENTORY LIMIT {ticker[-3:]} {side.upper()} - cancelling (inv: {inventory})")
                    del our_orders[key]
                except:
                    pass
            return

        if current and current.price == target_price:
            return  # No change needed

        # Cancel existing order if any (price is changing)
        if current:
            try:
                client.cancel_order(current.order_id)
                print(f"  Cancelled {ticker[-3:]} {side.upper()} @ {current.price}c")
            except:
                pass

        # Place new order at new price
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
            print(f"  Placed {ticker[-3:]} {side.upper()} @ {target_price}c")
        else:
            print(f"  ERROR placing {ticker[-3:]} {side.upper()}: {result}")

    # Track last book state to detect changes
    last_book_state = {}

    # Sticky ceiling: track when we last did a retest
    last_retest_time = datetime.datetime.now(datetime.timezone.utc)
    RETEST_INTERVAL = retest_interval  # seconds

    def update_quotes() -> bool:
        """Check book and update quotes to stay at top. Returns True if any order changed."""
        nonlocal last_book_state, last_retest_time, inventory

        now = datetime.datetime.now(datetime.timezone.utc)

        # Check if it's time for a retest (drop down to find better prices)
        is_retest = (now - last_retest_time).total_seconds() >= RETEST_INTERVAL
        if is_retest:
            print(f"\n[{now.strftime('%H:%M:%S')}] RETEST - dropping down to check for better fills | Inventory: {inventory}")
            last_retest_time = now

        # Inventory limits - determine which sides we can quote
        # inventory >= max: only quote PRX YES, RRQ NO (reduces inventory)
        # inventory <= -max: only quote RRQ YES, PRX NO (reduces inventory)
        can_quote_rrq_yes = inventory < inventory_max   # would increase inventory
        can_quote_prx_no = inventory < inventory_max    # would increase inventory
        can_quote_prx_yes = inventory > -inventory_max  # would decrease inventory
        can_quote_rrq_no = inventory > -inventory_max   # would decrease inventory

        # Must-quote mode: when at inventory limit, ALWAYS quote reducing sides at ceiling
        # This ensures we rebalance even if market is unfavorable
        at_positive_limit = inventory >= inventory_max   # long RRQ, need to reduce
        at_negative_limit = inventory <= -inventory_max  # long PRX, need to reduce
        must_quote_prx_yes = at_positive_limit  # PRX YES reduces positive inventory
        must_quote_rrq_no = at_positive_limit   # RRQ NO reduces positive inventory
        must_quote_rrq_yes = at_negative_limit  # RRQ YES reduces negative inventory
        must_quote_prx_no = at_negative_limit   # PRX NO reduces negative inventory

        # Get current orderbooks with depth
        prx_book = get_book_with_depth(PRX_TICKER)
        rrq_book = get_book_with_depth(RRQ_TICKER)

        # Get our current prices (if any)
        prx_yes_current = our_orders.get((PRX_TICKER, "yes"))
        rrq_yes_current = our_orders.get((RRQ_TICKER, "yes"))
        rrq_no_current = our_orders.get((RRQ_TICKER, "no"))
        prx_no_current = our_orders.get((PRX_TICKER, "no"))

        # Calculate adaptive prices for YES sides (with sticky ceiling)
        # If inventory limit reached, set to -2 (will cancel order)
        # If must_quote, force quoting at ceiling even if not competitive
        if can_quote_prx_yes:
            prx_yes_price = calculate_adaptive_price(
                prx_theo, prx_book["best_bid"], prx_book["second_bid"], "bid", edge_min,
                prx_yes_current.price if prx_yes_current else None,
                sticky_ceiling=True, is_retest=is_retest,
                best_qty=prx_book["best_bid_qty"], our_size=contracts,
                must_quote=must_quote_prx_yes)
        else:
            prx_yes_price = -2  # Cancel - at inventory limit

        if can_quote_rrq_yes:
            rrq_yes_price = calculate_adaptive_price(
                rrq_theo, rrq_book["best_bid"], rrq_book["second_bid"], "bid", edge_min,
                rrq_yes_current.price if rrq_yes_current else None,
                sticky_ceiling=True, is_retest=is_retest,
                best_qty=rrq_book["best_bid_qty"], our_size=contracts,
                must_quote=must_quote_rrq_yes)
        else:
            rrq_yes_price = -2  # Cancel - at inventory limit

        # For NO sides: use NO bid levels directly (with sticky ceiling)
        if can_quote_rrq_no:
            rrq_no_price = calculate_adaptive_price(
                prx_theo, rrq_book["best_no_bid"], rrq_book["second_no_bid"], "bid", edge_min,
                rrq_no_current.price if rrq_no_current else None,
                sticky_ceiling=True, is_retest=is_retest,
                best_qty=rrq_book["best_no_bid_qty"], our_size=contracts,
                must_quote=must_quote_rrq_no)
        else:
            rrq_no_price = -2  # Cancel - at inventory limit

        if can_quote_prx_no:
            prx_no_price = calculate_adaptive_price(
                rrq_theo, prx_book["best_no_bid"], prx_book["second_no_bid"], "bid", edge_min,
                prx_no_current.price if prx_no_current else None,
                sticky_ceiling=True, is_retest=is_retest,
                best_qty=prx_book["best_no_bid_qty"], our_size=contracts,
                must_quote=must_quote_prx_no)
        else:
            prx_no_price = -2  # Cancel - at inventory limit

        # Check what needs to change
        current_prices = {
            (PRX_TICKER, "yes"): prx_yes_price,
            (RRQ_TICKER, "yes"): rrq_yes_price,
            (RRQ_TICKER, "no"): rrq_no_price,
            (PRX_TICKER, "no"): prx_no_price,
        }

        changes_needed = []
        for key, target_price in current_prices.items():
            current = our_orders.get(key)
            if not current or current.price != target_price:
                changes_needed.append((key, target_price))

        if not changes_needed:
            return False  # Nothing to do

        # Print book state only when we're about to change something
        now = datetime.datetime.now(datetime.timezone.utc)
        rebalance_msg = ""
        if at_positive_limit:
            rebalance_msg = f" | REBALANCING (inv: +{inventory}, quoting PRX YES/RRQ NO at ceiling)"
        elif at_negative_limit:
            rebalance_msg = f" | REBALANCING (inv: {inventory}, quoting RRQ YES/PRX NO at ceiling)"
        print(f"\n[{now.strftime('%H:%M:%S')}] Book changed - re-quoting{rebalance_msg}")
        print(f"  PRX: {prx_book['best_bid']}c / {prx_book['best_ask']}c | RRQ: {rrq_book['best_bid']}c / {rrq_book['best_ask']}c")

        # Place/update orders that need changing
        place_or_update(PRX_TICKER, "yes", True, prx_yes_price)
        place_or_update(RRQ_TICKER, "yes", True, rrq_yes_price)
        place_or_update(RRQ_TICKER, "no", False, rrq_no_price)
        place_or_update(PRX_TICKER, "no", False, prx_no_price)

        return True

    # Cancel any existing orders before starting (clean slate)
    print("\nCancelling any existing orders...")
    cancelled = client.cancel_all_orders(ticker=PRX_TICKER)
    cancelled += client.cancel_all_orders(ticker=RRQ_TICKER)
    if cancelled > 0:
        print(f"  Cancelled {cancelled} existing orders")

    print("\nStarting adaptive loop... (Ctrl+C to stop)")
    print("Watching for book changes and fills...")
    print("-" * 60)

    # Initial quote placement
    update_quotes()

    try:
        last_status_time = datetime.datetime.now(datetime.timezone.utc)
        while True:
            now = datetime.datetime.now(datetime.timezone.utc)

            # Check if event started
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

                    # Check for NEW fills since last check
                    new_fills = fill_count - order.filled
                    if new_fills > 0:
                        # Update inventory based on what got filled
                        # RRQ YES / PRX NO → +new_fills (long RRQ)
                        # PRX YES / RRQ NO → -new_fills (long PRX)
                        ticker, side = key
                        if (ticker == RRQ_TICKER and side == "yes") or (ticker == PRX_TICKER and side == "no"):
                            inventory += new_fills
                        else:  # PRX YES or RRQ NO
                            inventory -= new_fills

                        if remaining_count == 0:
                            # Fully filled
                            print(f"\n[{now.strftime('%H:%M:%S')}] FILLED: {order.ticker[-3:]} {order.side.upper()} {fill_count}/{order.count} @ {order.price}c | Inventory: {inventory}")
                        else:
                            # Partial fill - cancel remaining, will re-quote full size
                            print(f"\n[{now.strftime('%H:%M:%S')}] PARTIAL: {order.ticker[-3:]} {order.side.upper()} {new_fills} filled @ {order.price}c ({remaining_count} cancelled) | Inventory: {inventory}")
                            try:
                                client.cancel_order(order.order_id)
                            except:
                                pass

                        # Remove from tracking - will re-quote
                        del our_orders[key]
                        fills_detected = True
                except:
                    pass

            # Update quotes (only prints if something changes)
            changed = update_quotes()

            # Print status every 60 seconds if nothing happened
            if not changed and not fills_detected:
                if (now - last_status_time).seconds >= 60:
                    time_to_event = event_time - now
                    our_prices = [f"{k[0][-3:]} {k[1].upper()}@{v.price}c" for k, v in our_orders.items()]
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


def monitor_and_requote(
    client: KalshiClient,
    contracts: int = 10,
    check_interval: int = 10,
    four_sided: bool = True
):
    """
    Monitor orders, re-quote on fills, cancel at event start.

    Args:
        client: Authenticated KalshiClient
        contracts: Contracts per order
        check_interval: Seconds between checks
        four_sided: Use 4-sided LOOP strategy
    """
    print("=" * 60)
    print("MARKET MAKER MONITOR")
    print("=" * 60)

    # Get event start time
    event_time = get_event_start_time(PRX_TICKER)
    if event_time:
        print(f"\nEvent start time: {event_time}")
        print(f"Current time:     {datetime.datetime.now(datetime.timezone.utc)}")
    else:
        print("\nWARNING: Could not get event start time!")
        event_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)

    # Track our positions
    positions = {"PRX_YES": 0, "PRX_NO": 0, "RRQ_YES": 0, "RRQ_NO": 0}

    print(f"\nMonitoring... (Ctrl+C to stop)")
    print(f"Check interval: {check_interval}s")
    print("-" * 60)

    try:
        while True:
            now = datetime.datetime.now(datetime.timezone.utc)

            # Check if event has started
            if now >= event_time:
                print(f"\n[{now.strftime('%H:%M:%S')}] EVENT STARTING - Cancelling all orders!")
                cancelled = client.cancel_all_orders(ticker=PRX_TICKER)
                cancelled += client.cancel_all_orders(ticker=RRQ_TICKER)
                print(f"Cancelled {cancelled} orders. Exiting.")
                break

            time_to_event = event_time - now
            print(f"\n[{now.strftime('%H:%M:%S')}] Time to event: {time_to_event}")

            # Get current theo
            theo = calculate_theo()

            # Check our resting orders
            prx_orders = client.get_orders(ticker=PRX_TICKER, status="resting")
            rrq_orders = client.get_orders(ticker=RRQ_TICKER, status="resting")

            print(f"  Resting orders: {len(prx_orders)} PRX, {len(rrq_orders)} RRQ")

            # Check for fills by looking at executed orders
            prx_fills = client.get_orders(ticker=PRX_TICKER, status="executed")
            rrq_fills = client.get_orders(ticker=RRQ_TICKER, status="executed")

            # Count current positions from fills
            new_positions = {"PRX_YES": 0, "PRX_NO": 0, "RRQ_YES": 0, "RRQ_NO": 0}
            for order in prx_fills:
                if order.get("action") == "buy":
                    key = "PRX_YES" if order.get("side") == "yes" else "PRX_NO"
                    new_positions[key] += order.get("fill_count", 0)
            for order in rrq_fills:
                if order.get("action") == "buy":
                    key = "RRQ_YES" if order.get("side") == "yes" else "RRQ_NO"
                    new_positions[key] += order.get("fill_count", 0)

            # Check if positions changed (new fills)
            if new_positions != positions:
                print(f"  NEW FILLS DETECTED!")
                print(f"    PRX YES: {positions['PRX_YES']} -> {new_positions['PRX_YES']}")
                print(f"    PRX NO:  {positions['PRX_NO']} -> {new_positions['PRX_NO']}")
                print(f"    RRQ YES: {positions['RRQ_YES']} -> {new_positions['RRQ_YES']}")
                print(f"    RRQ NO:  {positions['RRQ_NO']} -> {new_positions['RRQ_NO']}")
                positions = new_positions

                # Check if still profitable to re-quote
                prx_market = get_market_state(PRX_TICKER, "PRX")
                rrq_market = get_market_state(RRQ_TICKER, "RRQ")

                prx_prices = calculate_mm_prices(theo["PRX"], prx_market)
                rrq_prices = calculate_mm_prices(theo["RRQ"], rrq_market)

                total_cost = prx_prices['our_bid'] + rrq_prices['our_bid']
                profit = 100 - total_cost

                print(f"  Current theo: PRX {theo['PRX']}c, RRQ {theo['RRQ']}c")
                print(f"  Potential profit if re-quote: {profit}c per contract")

                if profit > 0:
                    print(f"  RE-QUOTING (still profitable)...")

                    # Cancel existing orders first
                    client.cancel_all_orders(ticker=PRX_TICKER)
                    client.cancel_all_orders(ticker=RRQ_TICKER)

                    # Place new orders
                    run_market_maker(
                        client=client,
                        contracts=contracts,
                        dry_run=False,
                        four_sided=four_sided
                    )
                else:
                    print(f"  NOT re-quoting (not profitable at current prices)")
            else:
                # Show current orderbook state
                prx_market = get_market_state(PRX_TICKER, "PRX")
                rrq_market = get_market_state(RRQ_TICKER, "RRQ")
                print(f"  PRX: {prx_market.best_bid}c / {prx_market.best_ask}c")
                print(f"  RRQ: {rrq_market.best_bid}c / {rrq_market.best_ask}c")

            time.sleep(check_interval)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        cancel = input("Cancel all orders? (y/n): ").strip().lower()
        if cancel == 'y':
            cancelled = client.cancel_all_orders(ticker=PRX_TICKER)
            cancelled += client.cancel_all_orders(ticker=RRQ_TICKER)
            print(f"Cancelled {cancelled} orders.")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RRQ vs PRX Market Maker")
    parser.add_argument("--live", action="store_true", help="Place real orders (requires auth)")
    parser.add_argument("--contracts", type=int, default=10, help="Contracts per order")
    parser.add_argument("--four-sided", action="store_true", help="Quote YES and NO on both markets (LOOP arb)")
    parser.add_argument("--prx-odds", type=float, help="Update PRX decimal odds")
    parser.add_argument("--rrq-odds", type=float, help="Update RRQ decimal odds")
    parser.add_argument("--cancel-all", action="store_true", help="Cancel all resting orders for this match")
    parser.add_argument("--monitor", action="store_true", help="Monitor fills, re-quote, auto-cancel at event start")
    parser.add_argument("--adaptive", action="store_true", help="Adaptive MM: stay top of book within theo ceiling")
    parser.add_argument("--edge", type=float, default=1.0, help="Min edge from theo in cents (default: 1.0)")
    parser.add_argument("--interval", type=float, default=2.0, help="Check interval in seconds (default: 2.0)")
    parser.add_argument("--retest", type=int, default=300, help="Retest interval in seconds - drop down from ceiling to find better fills (default: 300 = 5 min)")
    parser.add_argument("--inventory-max", type=int, default=50, help="Max inventory exposure in either direction (default: 50)")

    args = parser.parse_args()

    # Update odds if provided
    if args.prx_odds and args.rrq_odds:
        update_odds(args.prx_odds, args.rrq_odds, "cli")

    # Load credentials if needed
    client = None
    if args.live or args.cancel_all or args.monitor or args.adaptive:
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

    if args.cancel_all:
        print("\nCancelling all orders for this match...")
        cancelled = client.cancel_all_orders(ticker=PRX_TICKER)
        cancelled += client.cancel_all_orders(ticker=RRQ_TICKER)
        print(f"Cancelled {cancelled} orders.")
    elif args.adaptive:
        # Feb 15, 7:00 UTC = 2 AM EST (match start)
        event_time = datetime.datetime(2026, 2, 15, 7, 0, 0, tzinfo=datetime.timezone.utc)
        adaptive_market_maker(
            client=client,
            contracts=args.contracts,
            edge_min=args.edge,
            check_interval=args.interval,
            event_time=event_time,
            retest_interval=args.retest,
            inventory_max=args.inventory_max
        )
    elif args.monitor:
        monitor_and_requote(
            client=client,
            contracts=args.contracts,
            check_interval=args.interval,
            four_sided=args.four_sided
        )
    elif args.live:
        run_market_maker(client=client, contracts=args.contracts, dry_run=False, four_sided=args.four_sided)
    else:
        run_market_maker(dry_run=True, contracts=args.contracts, four_sided=args.four_sided)
