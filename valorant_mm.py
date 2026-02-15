"""
Valorant Market Maker - Scan Kalshi Valorant markets for opportunities.

Usage:
    python valorant_mm.py              # Scan markets
    python valorant_mm.py --theo       # Interactive theo entry mode
"""

import requests
import argparse
from dataclasses import dataclass
from typing import Optional

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Bookmaker weights for theo calculation
BOOKMAKER_WEIGHTS = {
    "pinnacle": 1.0,      # Sharpest
    "bet365": 0.8,
    "betway": 0.7,
    "ggbet": 0.6,
    "1xbet": 0.5,
    "default": 0.5
}

# OddsPapi config - sign up at https://oddspapi.io for free API key (200 req/month)
ODDSPAPI_BASE = "https://api.oddspapi.io/v4"
VALORANT_SPORT_ID = 61  # Valorant sport ID on OddsPapi


def get_oddspapi_key() -> Optional[str]:
    """Get OddsPapi API key from config or environment."""
    try:
        from config.config import ODDSPAPI_API_KEY
        return ODDSPAPI_API_KEY
    except ImportError:
        import os
        return os.environ.get("ODDSPAPI_API_KEY")


def fetch_valorant_odds(api_key: str) -> list[dict]:
    """Fetch Valorant match odds from OddsPapi."""
    url = f"{ODDSPAPI_BASE}/fixtures"
    params = {
        "apiKey": api_key,
        "sportId": VALORANT_SPORT_ID,
        "hasOdds": "true"
    }

    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        print(f"OddsPapi error: {resp.status_code} - {resp.text[:200]}")
        return []

    return resp.json().get("data", [])


def american_to_prob(odds: int) -> float:
    """Convert American odds to implied probability."""
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return -odds / (-odds + 100)


def decimal_to_prob(odds: float) -> float:
    """Convert decimal odds to implied probability."""
    return 1 / odds if odds > 0 else 0


def calculate_theo_from_odds(odds_data: list[dict], team_name: str) -> Optional[float]:
    """
    Calculate theo probability for a team from bookmaker odds.
    Returns probability as decimal (0-1).
    """
    weighted_sum = 0
    total_weight = 0

    for bookmaker_odds in odds_data:
        book_name = bookmaker_odds.get("bookmaker", "").lower()
        outcomes = bookmaker_odds.get("outcomes", [])

        for outcome in outcomes:
            if team_name.lower() in outcome.get("name", "").lower():
                odds_value = outcome.get("odds")
                if odds_value:
                    # Assume decimal odds
                    prob = decimal_to_prob(float(odds_value))
                    weight = BOOKMAKER_WEIGHTS.get(book_name, BOOKMAKER_WEIGHTS["default"])
                    weighted_sum += prob * weight
                    total_weight += weight
                    break

    if total_weight == 0:
        return None

    # Remove vig (~5%)
    raw_prob = weighted_sum / total_weight
    return raw_prob / 1.05


def match_kalshi_to_odds(kalshi_markets: list, odds_fixtures: list) -> list[dict]:
    """
    Match Kalshi Valorant markets to OddsPapi fixtures.
    Returns list of matched opportunities with theo prices.
    """
    matched = []

    for fixture in odds_fixtures:
        home_team = fixture.get("homeTeam", {}).get("name", "")
        away_team = fixture.get("awayTeam", {}).get("name", "")

        # Find matching Kalshi markets
        for km in kalshi_markets:
            title = km.title.lower()
            if (home_team.lower() in title or away_team.lower() in title):
                # Found a match - calculate theo
                odds_data = fixture.get("odds", [])
                theo = calculate_theo_from_odds(odds_data, km.team)

                matched.append({
                    "kalshi": km,
                    "fixture": fixture,
                    "theo": theo,
                    "edge": (km.mid / 100 - theo) if theo else None
                })

    return matched


@dataclass
class ValorantMarket:
    ticker: str
    title: str
    team: str
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int
    spread: int
    yes_depth: int
    no_depth: int
    volume: int

    @property
    def mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2

    def __str__(self):
        return (
            f"{self.title[:50]}\n"
            f"  Ticker: {self.ticker}\n"
            f"  YES: {self.yes_bid}c bid / {self.yes_ask}c ask (spread: {self.yes_ask - self.yes_bid}c)\n"
            f"  NO:  {self.no_bid}c bid / {self.no_ask}c ask (spread: {self.no_ask - self.no_bid}c)\n"
            f"  Mid: {self.mid:.1f}c | Volume: {self.volume} | Depth: {self.yes_depth}/{self.no_depth}"
        )


def get_valorant_markets() -> list[dict]:
    """Get all open Valorant game markets."""
    url = f"{BASE_URL}/markets"
    params = {
        "series_ticker": "KXVALORANTGAME",
        "status": "open",
        "limit": 100
    }

    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        print(f"Error: {resp.status_code}")
        return []

    return resp.json().get("markets", [])


def get_orderbook(ticker: str) -> Optional[dict]:
    """Get orderbook for a market."""
    url = f"{BASE_URL}/markets/{ticker}/orderbook"
    resp = requests.get(url)
    if resp.status_code != 200:
        return None
    return resp.json().get("orderbook", {})


def analyze_valorant_market(market: dict) -> Optional[ValorantMarket]:
    """Analyze a Valorant market."""
    ticker = market.get("ticker")
    orderbook = get_orderbook(ticker)

    if not orderbook:
        return None

    yes_bids = orderbook.get("yes") or []
    no_bids = orderbook.get("no") or []

    if not yes_bids and not no_bids:
        return None

    best_yes_bid = max([b[0] for b in yes_bids], default=0)
    best_no_bid = max([b[0] for b in no_bids], default=0)

    best_yes_ask = 100 - best_no_bid if best_no_bid > 0 else 100
    best_no_ask = 100 - best_yes_bid if best_yes_bid > 0 else 100

    spread = min(best_yes_ask - best_yes_bid, best_no_ask - best_no_bid)

    # Extract team name from ticker (last part after last dash)
    team = ticker.split("-")[-1] if "-" in ticker else ""

    return ValorantMarket(
        ticker=ticker,
        title=market.get("title", ""),
        team=team,
        yes_bid=best_yes_bid,
        yes_ask=best_yes_ask,
        no_bid=best_no_bid,
        no_ask=best_no_ask,
        spread=spread,
        yes_depth=sum(b[1] for b in yes_bids),
        no_depth=sum(b[1] for b in no_bids),
        volume=market.get("volume", 0)
    )


def find_match_pairs(markets: list[ValorantMarket]) -> list[tuple[ValorantMarket, ValorantMarket]]:
    """
    Find pairs of markets for the same match (Team A vs Team B).
    Returns list of (team_a_market, team_b_market) tuples.
    """
    # Group by match (remove team suffix from ticker)
    by_match = {}
    for m in markets:
        # Ticker format: KXVALORANTGAME-26FEB14VITTL-VIT
        # Match key: KXVALORANTGAME-26FEB14VITTL
        parts = m.ticker.rsplit("-", 1)
        if len(parts) == 2:
            match_key = parts[0]
            if match_key not in by_match:
                by_match[match_key] = []
            by_match[match_key].append(m)

    pairs = []
    for match_key, match_markets in by_match.items():
        if len(match_markets) == 2:
            pairs.append((match_markets[0], match_markets[1]))

    return pairs


def scan_opportunities(min_spread: int = 3, max_spread: int = 20):
    """Scan for market making opportunities."""
    print("Fetching Valorant markets...")
    markets = get_valorant_markets()
    print(f"Found {len(markets)} open markets\n")

    if not markets:
        return []

    print("Analyzing orderbooks...")
    analyzed = []
    for m in markets:
        result = analyze_valorant_market(m)
        if result and result.yes_bid > 0 and result.no_bid > 0:
            analyzed.append(result)

    print(f"Analyzed {len(analyzed)} markets with two-sided quotes\n")

    # Find match pairs
    pairs = find_match_pairs(analyzed)

    print("=" * 70)
    print("VALORANT MATCH OPPORTUNITIES")
    print("=" * 70)

    opportunities = []

    for team_a, team_b in pairs:
        # Check if prices are consistent (should sum to ~100)
        implied_sum = team_a.mid + team_b.mid

        print(f"\n{team_a.title}")
        print(f"  {team_a.team}: {team_a.yes_bid}c / {team_a.yes_ask}c (spread: {team_a.yes_ask - team_a.yes_bid}c)")
        print(f"  {team_b.team}: {team_b.yes_bid}c / {team_b.yes_ask}c (spread: {team_b.yes_ask - team_b.yes_bid}c)")
        print(f"  Implied sum: {implied_sum:.1f}c (should be ~100)")

        avg_spread = (team_a.spread + team_b.spread) / 2
        if min_spread <= avg_spread <= max_spread:
            opportunities.append((team_a, team_b, avg_spread))
            print(f"  >>> OPPORTUNITY: Avg spread {avg_spread:.1f}c")

    # Also show individual markets sorted by spread
    print("\n" + "=" * 70)
    print("ALL MARKETS BY SPREAD")
    print("=" * 70)

    analyzed.sort(key=lambda x: x.spread)
    for m in analyzed[:20]:
        print(f"\n{m.spread}c spread | {m.title[:45]}")
        print(f"  YES: {m.yes_bid}c / {m.yes_ask}c | Vol: {m.volume}")
        print(f"  Ticker: {m.ticker}")

    return opportunities


def scan_with_odds():
    """Scan markets and match with OddsPapi odds for theo calculation."""
    api_key = get_oddspapi_key()

    if not api_key:
        print("No OddsPapi API key found!")
        print("Add ODDSPAPI_API_KEY to config/config.py or set as environment variable")
        print("Sign up free at: https://oddspapi.io (200 requests/month)")
        return

    print("Fetching Kalshi Valorant markets...")
    markets = get_valorant_markets()

    analyzed = []
    for m in markets:
        result = analyze_valorant_market(m)
        if result:
            analyzed.append(result)

    print(f"Found {len(analyzed)} Kalshi markets")

    print("\nFetching OddsPapi Valorant odds...")
    odds_fixtures = fetch_valorant_odds(api_key)
    print(f"Found {len(odds_fixtures)} fixtures with odds")

    if not odds_fixtures:
        print("No odds data available. Try again later or check API key.")
        return

    print("\nMatching markets to odds...")
    matched = match_kalshi_to_odds(analyzed, odds_fixtures)

    print("\n" + "=" * 70)
    print("OPPORTUNITIES WITH THEO")
    print("=" * 70)

    for m in matched:
        km = m["kalshi"]
        theo = m["theo"]
        edge = m["edge"]

        print(f"\n{km.title[:50]}")
        print(f"  Kalshi: {km.yes_bid}c / {km.yes_ask}c (mid: {km.mid:.1f}c)")

        if theo:
            theo_cents = theo * 100
            print(f"  Theo:   {theo_cents:.1f}c")
            print(f"  Edge:   {edge * 100:+.1f}c")

            # Check if there's an opportunity
            if km.yes_ask < theo_cents:
                print(f"  >>> BUY YES at {km.yes_ask}c (theo {theo_cents:.1f}c)")
            elif km.yes_bid > theo_cents:
                print(f"  >>> SELL YES at {km.yes_bid}c (theo {theo_cents:.1f}c)")
        else:
            print(f"  Theo:   N/A (no odds match)")


def interactive_theo_mode():
    """Interactive mode for manual theo entry."""
    print("Fetching Valorant markets...")
    markets = get_valorant_markets()

    analyzed = []
    for m in markets:
        result = analyze_valorant_market(m)
        if result and result.yes_bid > 0 and result.no_bid > 0:
            analyzed.append(result)

    pairs = find_match_pairs(analyzed)

    print(f"\nFound {len(pairs)} matches. Enter theo probabilities from Pinnacle/other books.\n")

    for team_a, team_b in pairs:
        print("=" * 60)
        print(f"Match: {team_a.team} vs {team_b.team}")
        print(f"  {team_a.team}: Kalshi {team_a.yes_bid}c / {team_a.yes_ask}c (mid: {team_a.mid:.1f}c)")
        print(f"  {team_b.team}: Kalshi {team_b.yes_bid}c / {team_b.yes_ask}c (mid: {team_b.mid:.1f}c)")

        try:
            theo_input = input(f"\nEnter theo for {team_a.team} (0-100, or 'skip'): ").strip()
            if theo_input.lower() == 'skip':
                continue

            theo_a = float(theo_input)
            theo_b = 100 - theo_a

            print(f"\nAnalysis:")
            print(f"  {team_a.team}: Theo {theo_a:.1f}c | Kalshi mid {team_a.mid:.1f}c | Edge: {team_a.mid - theo_a:+.1f}c")
            print(f"  {team_b.team}: Theo {theo_b:.1f}c | Kalshi mid {team_b.mid:.1f}c | Edge: {team_b.mid - theo_b:+.1f}c")

            # Check for opportunities
            if team_a.yes_ask < theo_a:
                print(f"  >>> BUY {team_a.team} YES at {team_a.yes_ask}c (theo {theo_a:.1f}c) = +{theo_a - team_a.yes_ask:.1f}c EV")
            if team_a.yes_bid > theo_a:
                print(f"  >>> SELL {team_a.team} YES at {team_a.yes_bid}c (theo {theo_a:.1f}c) = +{team_a.yes_bid - theo_a:.1f}c EV")
            if team_b.yes_ask < theo_b:
                print(f"  >>> BUY {team_b.team} YES at {team_b.yes_ask}c (theo {theo_b:.1f}c) = +{theo_b - team_b.yes_ask:.1f}c EV")
            if team_b.yes_bid > theo_b:
                print(f"  >>> SELL {team_b.team} YES at {team_b.yes_bid}c (theo {theo_b:.1f}c) = +{team_b.yes_bid - theo_b:.1f}c EV")

        except ValueError:
            print("Invalid input, skipping...")
            continue

        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Valorant Market Maker")
    parser.add_argument("--theo", action="store_true", help="Interactive theo entry mode")
    parser.add_argument("--odds", action="store_true", help="Fetch odds from OddsPapi")

    args = parser.parse_args()

    if args.theo:
        interactive_theo_mode()
    elif args.odds:
        scan_with_odds()
    else:
        opportunities = scan_opportunities(min_spread=3, max_spread=25)
        print("\n" + "=" * 70)
        print(f"SUMMARY: Found {len(opportunities)} match opportunities")
        print("=" * 70)
        print("\nNext steps:")
        print("  python valorant_mm.py --theo   # Manual theo entry mode")
        print("  python valorant_mm.py --odds   # Auto-fetch from OddsPapi")
