"""
Market Scanner - Find Kalshi sports markets with wide spreads for market making opportunities.
"""

import requests
import sys
from typing import Optional
from dataclasses import dataclass

# Fix Windows console encoding
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except:
    pass

def safe_str(s: str) -> str:
    """Remove non-ASCII characters for safe printing."""
    return s.encode('ascii', 'ignore').decode('ascii') if s else ""

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass
class MarketSpread:
    ticker: str
    title: str
    series_ticker: str
    best_yes_bid: int  # cents
    best_no_bid: int   # cents
    best_yes_ask: int  # cents (100 - best_no_bid)
    best_no_ask: int   # cents (100 - best_yes_bid)
    spread: int        # cents (yes_ask - yes_bid OR no_ask - no_bid)
    yes_depth: int     # total contracts on yes side
    no_depth: int      # total contracts on no side
    volume: int
    open_interest: int


def get_sports_series() -> list[dict]:
    """Get all series in the sports category."""
    url = f"{BASE_URL}/series"
    params = {"limit": 200}

    all_series = []
    cursor = None

    while True:
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(url, params=params)
        if resp.status_code != 200:
            print(f"Error fetching series: {resp.status_code} - {resp.text}")
            break

        data = resp.json()
        series_list = data.get("series", [])

        # Filter for sports-related series
        sports_keywords = ["nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball",
                          "baseball", "hockey", "tennis", "golf", "ufc", "mma", "boxing",
                          "ncaa", "college", "premier", "champions", "world cup", "sports"]

        for s in series_list:
            title_lower = s.get("title", "").lower()
            ticker_lower = s.get("ticker", "").lower()
            category = s.get("category", "").lower()

            # Check if it's sports-related
            is_sports = (
                category == "sports" or
                any(kw in title_lower for kw in sports_keywords) or
                any(kw in ticker_lower for kw in sports_keywords)
            )

            if is_sports:
                all_series.append(s)

        cursor = data.get("cursor")
        if not cursor:
            break

    return all_series


def get_open_markets(series_ticker: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Get open markets, optionally filtered by series."""
    url = f"{BASE_URL}/markets"
    params = {
        "status": "open",
        "limit": min(limit, 1000)
    }

    if series_ticker:
        params["series_ticker"] = series_ticker

    all_markets = []
    cursor = None

    while len(all_markets) < limit:
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(url, params=params)
        if resp.status_code != 200:
            print(f"Error fetching markets: {resp.status_code} - {resp.text}")
            break

        data = resp.json()
        markets = data.get("markets", [])
        all_markets.extend(markets)

        cursor = data.get("cursor")
        if not cursor or not markets:
            break

    return all_markets[:limit]


def get_orderbook(ticker: str) -> Optional[dict]:
    """Fetch orderbook for a market."""
    url = f"{BASE_URL}/markets/{ticker}/orderbook"

    resp = requests.get(url)
    if resp.status_code != 200:
        return None

    return resp.json().get("orderbook", {})


def analyze_market_spread(market: dict) -> Optional[MarketSpread]:
    """Analyze a market's spread and depth."""
    ticker = market.get("ticker")

    orderbook = get_orderbook(ticker)
    if not orderbook:
        return None

    yes_bids = orderbook.get("yes") or []  # [[price, quantity], ...]
    no_bids = orderbook.get("no") or []

    if not yes_bids and not no_bids:
        return None

    # Best bids (highest price someone will pay)
    best_yes_bid = max([b[0] for b in yes_bids], default=0)
    best_no_bid = max([b[0] for b in no_bids], default=0)

    # Best asks (derived: if best YES bid is 45, best NO ask is 55)
    best_yes_ask = 100 - best_no_bid if best_no_bid > 0 else 100
    best_no_ask = 100 - best_yes_bid if best_yes_bid > 0 else 100

    # Spread calculation
    yes_spread = best_yes_ask - best_yes_bid if best_yes_bid > 0 else 100
    no_spread = best_no_ask - best_no_bid if best_no_bid > 0 else 100
    spread = min(yes_spread, no_spread)

    # Depth (total contracts available)
    yes_depth = sum([b[1] for b in yes_bids])
    no_depth = sum([b[1] for b in no_bids])

    return MarketSpread(
        ticker=ticker,
        title=market.get("title", ""),
        series_ticker=market.get("series_ticker", ""),
        best_yes_bid=best_yes_bid,
        best_no_bid=best_no_bid,
        best_yes_ask=best_yes_ask,
        best_no_ask=best_no_ask,
        spread=spread,
        yes_depth=yes_depth,
        no_depth=no_depth,
        volume=market.get("volume", 0),
        open_interest=market.get("open_interest", 0)
    )


def scan_sports_markets(
    min_spread: int = 5,
    max_spread: int = 50,
    min_volume: int = 0,
    min_depth: int = 0,
    require_both_sides: bool = False,
    max_markets: int = 500
) -> list[MarketSpread]:
    """
    Scan all sports markets and find ones with tradeable spreads.

    Args:
        min_spread: Minimum spread in cents to include
        max_spread: Maximum spread (filter out illiquid 98¢ spread markets)
        min_volume: Minimum trading volume
        min_depth: Minimum depth on each side
        require_both_sides: Require bids on both YES and NO
        max_markets: Maximum number of markets to scan

    Returns:
        List of MarketSpread objects sorted by spread (widest first)
    """
    print("Fetching sports series...")
    sports_series = get_sports_series()
    print(f"Found {len(sports_series)} sports series")

    if not sports_series:
        print("No sports series found. Scanning all open markets instead...")
        markets = get_open_markets(limit=max_markets)
    else:
        # Get markets from each sports series
        markets = []
        for series in sports_series:
            series_ticker = series.get("ticker")
            print(f"  Scanning {series_ticker}: {safe_str(series.get('title', ''))[:50]}")
            series_markets = get_open_markets(series_ticker=series_ticker, limit=100)
            markets.extend(series_markets)

            if len(markets) >= max_markets:
                break

        markets = markets[:max_markets]

    print(f"\nAnalyzing {len(markets)} markets...")

    results = []
    for i, market in enumerate(markets):
        if (i + 1) % 20 == 0:
            print(f"  Progress: {i + 1}/{len(markets)}")

        spread_info = analyze_market_spread(market)
        if spread_info:
            # Filter criteria
            if spread_info.spread < min_spread:
                continue
            if spread_info.spread > max_spread:
                continue
            if spread_info.volume < min_volume:
                continue
            if require_both_sides and (spread_info.best_yes_bid == 0 or spread_info.best_no_bid == 0):
                continue
            if spread_info.yes_depth < min_depth or spread_info.no_depth < min_depth:
                continue
            results.append(spread_info)

    # Sort by spread (widest first)
    results.sort(key=lambda x: x.spread, reverse=True)

    return results


def print_opportunities(opportunities: list[MarketSpread], top_n: int = 20):
    """Print market making opportunities in a readable format."""
    print("\n" + "=" * 100)
    print(f"TOP {min(top_n, len(opportunities))} MARKET MAKING OPPORTUNITIES (sorted by spread)")
    print("=" * 100)

    for i, opp in enumerate(opportunities[:top_n], 1):
        print(f"\n{i}. {safe_str(opp.title)[:70]}")
        print(f"   Ticker: {opp.ticker}")
        print(f"   Series: {opp.series_ticker}")
        print(f"   ┌─────────────────────────────────────────────────────")
        print(f"   │ YES: Bid {opp.best_yes_bid:2d}¢ | Ask {opp.best_yes_ask:2d}¢ | Spread: {opp.best_yes_ask - opp.best_yes_bid:2d}¢")
        print(f"   │ NO:  Bid {opp.best_no_bid:2d}¢ | Ask {opp.best_no_ask:2d}¢ | Spread: {opp.best_no_ask - opp.best_no_bid:2d}¢")
        print(f"   └─────────────────────────────────────────────────────")
        print(f"   Depth: {opp.yes_depth} YES / {opp.no_depth} NO | Volume: {opp.volume} | OI: {opp.open_interest}")


def find_all_categories() -> dict:
    """Utility: Find all unique categories on Kalshi."""
    url = f"{BASE_URL}/series"
    params = {"limit": 200}

    categories = {}
    cursor = None

    while True:
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(url, params=params)
        if resp.status_code != 200:
            break

        data = resp.json()
        for s in data.get("series", []):
            cat = s.get("category", "unknown")
            if cat not in categories:
                categories[cat] = []
            categories[cat].append({
                "ticker": s.get("ticker"),
                "title": s.get("title")
            })

        cursor = data.get("cursor")
        if not cursor:
            break

    return categories


def scan_active_game_markets(max_markets: int = 500) -> list[MarketSpread]:
    """
    Scan for active game-day markets that are TRULY BINARY (no ties).
    Excludes soccer match winners (3-way: home/away/draw).
    """
    # Series tickers for BINARY markets only
    active_game_series = [
        # Esports - typically liquid with tight spreads
        "KXCOD", "KXLOLMATCH", "KXCSGOMATCH", "KXVALORANT", "KXDOTA2",
        "KXLOLCHAMP", "KXCSGOMAJOR",
        # NBA - no ties, binary
        "KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBA1HTOTAL", "KXNBA3QTOTAL",
        # NHL - has OT/shootout, effectively binary for betting
        "KXNHLGAME", "KXNHLTOTAL", "KXNHLSPREAD",
        # Soccer TOTALS only (over/under is binary) - NOT match winners
        "KXEPLTOTAL", "KXUCLTOTAL", "KXFACUPTOTAL", "KXLALIGATOTAL",
        # Soccer "to advance" (binary - one team advances)
        "KXFACUPADVANCE", "KXUCLADVANCE",
        # College Basketball - no ties, binary
        "KXNCAAMBGAME", "KXNCAAMBTOTAL",
        # Tennis - no ties, binary
        "KXATPMATCH", "KXWTAMATCH",
        # Table Tennis (very active)
        "KXTTELITEGAME",
    ]

    print("Scanning active game-day markets...")
    all_markets = []

    for series_ticker in active_game_series:
        markets = get_open_markets(series_ticker=series_ticker, limit=50)
        if markets:
            print(f"  {series_ticker}: {len(markets)} open markets")
            all_markets.extend(markets)

        if len(all_markets) >= max_markets:
            break

    print(f"\nAnalyzing {len(all_markets)} game markets...")

    results = []
    for i, market in enumerate(all_markets[:max_markets]):
        if (i + 1) % 20 == 0:
            print(f"  Progress: {i + 1}/{min(len(all_markets), max_markets)}")

        spread_info = analyze_market_spread(market)
        if spread_info and spread_info.spread >= 3:
            results.append(spread_info)

    results.sort(key=lambda x: x.spread, reverse=True)
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scan Kalshi sports markets for trading opportunities")
    parser.add_argument("--mode", choices=["all", "active", "categories"], default="active",
                       help="Scan mode: 'all' for all sports, 'active' for game-day markets, 'categories' to list categories")
    parser.add_argument("--min-spread", type=int, default=5, help="Minimum spread in cents")
    parser.add_argument("--max-spread", type=int, default=40, help="Maximum spread in cents")
    parser.add_argument("--min-volume", type=int, default=0, help="Minimum volume")
    parser.add_argument("--require-both", action="store_true", help="Require bids on both sides")
    parser.add_argument("--max-markets", type=int, default=500, help="Max markets to scan")

    args = parser.parse_args()

    if args.mode == "categories":
        print("Discovering all Kalshi categories...\n")
        categories = find_all_categories()
        print("Categories found:")
        for cat, series_list in sorted(categories.items()):
            print(f"  {cat}: {len(series_list)} series")
            # Show a few example series
            for s in series_list[:3]:
                print(f"    - {s['ticker']}: {safe_str(s['title'])[:60]}")
            if len(series_list) > 3:
                print(f"    ... and {len(series_list) - 3} more")

    elif args.mode == "active":
        # Scan game-day markets
        opportunities = scan_active_game_markets(max_markets=args.max_markets)

        # Also filter by criteria
        filtered = [o for o in opportunities
                   if o.spread >= args.min_spread
                   and o.spread <= args.max_spread
                   and o.volume >= args.min_volume
                   and (not args.require_both or (o.best_yes_bid > 0 and o.best_no_bid > 0))]

        if filtered:
            print_opportunities(filtered, top_n=30)
        else:
            print("\nNo active game markets found matching criteria.")
            print("Showing all found markets instead:")
            print_opportunities(opportunities, top_n=30)

    else:
        # Full scan
        print("Discovering all Kalshi categories...\n")
        categories = find_all_categories()
        print("Categories found:")
        for cat, series_list in sorted(categories.items()):
            print(f"  {cat}: {len(series_list)} series")
        print("\n" + "-" * 50)

        opportunities = scan_sports_markets(
            min_spread=args.min_spread,
            max_spread=args.max_spread,
            min_volume=args.min_volume,
            require_both_sides=args.require_both,
            max_markets=args.max_markets
        )

        if opportunities:
            print_opportunities(opportunities, top_n=30)

            print("\n" + "=" * 100)
            print("SUMMARY")
            print("=" * 100)
            print(f"Total tradeable markets found: {len(opportunities)}")

            # Group by series
            series_counts = {}
            for opp in opportunities:
                series_counts[opp.series_ticker] = series_counts.get(opp.series_ticker, 0) + 1

            print("\nOpportunities by series:")
            for series, count in sorted(series_counts.items(), key=lambda x: -x[1])[:15]:
                print(f"  {series}: {count}")
        else:
            print("\nNo opportunities found with the specified criteria.")
