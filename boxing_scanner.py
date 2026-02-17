"""
Boxing market scanner - matches Kalshi markets with Odds API odds.
Handles 3-way odds (fighter A / draw / fighter B) where draw resolves 50/50 on Kalshi.
"""

import requests
from dataclasses import dataclass
from typing import Optional
from config.config import ODDSAPI_API_KEY

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"


@dataclass
class BoxingMatch:
    """A boxing match with Kalshi tickers and odds."""
    fighter_a: str
    fighter_b: str
    ticker_a: str
    ticker_b: str
    odds_a: float  # decimal, from Odds API
    odds_b: float
    odds_draw: float  # draw odds
    bookmakers: list[str]
    commence_time: str

    @property
    def implied_a(self) -> float:
        return 1 / self.odds_a

    @property
    def implied_b(self) -> float:
        return 1 / self.odds_b

    @property
    def implied_draw(self) -> float:
        return 1 / self.odds_draw if self.odds_draw > 0 else 0

    @property
    def total_implied(self) -> float:
        return self.implied_a + self.implied_b + self.implied_draw

    @property
    def novig_a_raw(self) -> float:
        """No-vig probability for fighter A win (excluding draw)."""
        return self.implied_a / self.total_implied

    @property
    def novig_b_raw(self) -> float:
        """No-vig probability for fighter B win (excluding draw)."""
        return self.implied_b / self.total_implied

    @property
    def novig_draw(self) -> float:
        """No-vig probability for draw."""
        return self.implied_draw / self.total_implied

    @property
    def theo_a(self) -> int:
        """No-vig theo for fighter A in cents (draw splits 50/50)."""
        return int(round((self.novig_a_raw + self.novig_draw / 2) * 100))

    @property
    def theo_b(self) -> int:
        """No-vig theo for fighter B in cents (draw splits 50/50)."""
        return 100 - self.theo_a

    @property
    def fair_odds_a(self) -> float:
        """Fair decimal odds for fighter A (no vig, draw-adjusted)."""
        return 100 / self.theo_a if self.theo_a > 0 else 99

    @property
    def fair_odds_b(self) -> float:
        """Fair decimal odds for fighter B (no vig, draw-adjusted)."""
        return 100 / self.theo_b if self.theo_b > 0 else 99

    def __str__(self):
        draw_pct = f" (draw: {self.novig_draw*100:.1f}%)" if self.odds_draw > 0 else ""
        return (
            f"{self.fighter_a} vs {self.fighter_b}\n"
            f"  Time: {self.commence_time[:16]}\n"
            f"  Raw odds: {self.odds_a:.2f} / {self.odds_b:.2f} / draw {self.odds_draw:.2f}{draw_pct}\n"
            f"  Theo: {self.theo_a}c / {self.theo_b}c\n"
            f"  Ticker A: {self.ticker_a}\n"
            f"  Ticker B: {self.ticker_b}\n"
            f"  Dashboard odds: {self.fair_odds_a:.2f} / {self.fair_odds_b:.2f}"
        )


def normalize_name(name: str) -> str:
    """Normalize fighter name for matching."""
    # Remove common suffixes and clean up
    name = name.lower()
    for suffix in [" jr", " jr.", " sr", " sr.", " iii", " ii", " iv"]:
        name = name.replace(suffix, "")
    return name.strip()


def get_name_parts(name: str) -> list[str]:
    """Get significant parts of a name for matching."""
    normalized = normalize_name(name)
    parts = normalized.split()
    # Return last name and first name separately
    return [p for p in parts if len(p) > 2]


def get_kalshi_markets() -> dict:
    """Get all Kalshi boxing markets, grouped by event."""
    r = requests.get(f"{KALSHI_BASE}/markets",
        params={"series_ticker": "KXBOXING", "limit": 200})
    # Filter to active only
    markets = [m for m in r.json().get("markets", []) if m.get("status") == "active"]

    # Group by event
    events = {}
    for m in markets:
        base = m["ticker"].rsplit("-", 1)[0]
        team_code = m["ticker"].rsplit("-", 1)[1]
        if base not in events:
            events[base] = {"title": m["title"], "tickers": {}}
        events[base]["tickers"][team_code] = m["ticker"]

    return events


def get_odds_api_events() -> list:
    """Get boxing odds from The Odds API."""
    r = requests.get(f"{ODDS_API_BASE}/sports/boxing_boxing/odds",
        params={
            "apiKey": ODDSAPI_API_KEY,
            "regions": "us,us2,uk,eu",
            "markets": "h2h",
            "oddsFormat": "decimal"
        })
    return r.json()


def match_events(kalshi_events: dict, odds_events: list) -> list[BoxingMatch]:
    """Match Kalshi markets with Odds API events."""
    matches = []

    for odds_event in odds_events:
        home = odds_event["home_team"]
        away = odds_event["away_team"]
        home_parts = get_name_parts(home)
        away_parts = get_name_parts(away)

        # Find matching Kalshi event
        for base, data in kalshi_events.items():
            title_lower = data["title"].lower()

            # Check if both fighters appear in title (match on last names typically)
            home_match = any(p in title_lower for p in home_parts)
            away_match = any(p in title_lower for p in away_parts)

            if home_match and away_match:
                # Get best odds for each outcome
                best_home = best_away = 0
                best_draw = 0
                bookmakers = []

                for bm in odds_event.get("bookmakers", []):
                    for outcome in bm["markets"][0]["outcomes"]:
                        if outcome["name"] == home and outcome["price"] > best_home:
                            best_home = outcome["price"]
                        elif outcome["name"] == away and outcome["price"] > best_away:
                            best_away = outcome["price"]
                        elif outcome["name"].lower() == "draw" and outcome["price"] > best_draw:
                            best_draw = outcome["price"]
                    if bm["key"] not in bookmakers:
                        bookmakers.append(bm["key"])

                if best_home > 0 and best_away > 0:
                    # Figure out which ticker is which fighter
                    tickers = data["tickers"]

                    ticker_home = ticker_away = None
                    for code, ticker in tickers.items():
                        code_lower = code.lower()
                        # Check if code matches any part of fighter's name
                        if any(code_lower in p or p.startswith(code_lower[:3]) for p in home_parts):
                            ticker_home = ticker
                        elif any(code_lower in p or p.startswith(code_lower[:3]) for p in away_parts):
                            ticker_away = ticker

                    # Fallback: assign unmatched ticker
                    all_tickers = list(tickers.values())
                    if not ticker_home and ticker_away:
                        ticker_home = [t for t in all_tickers if t != ticker_away][0]
                    elif not ticker_away and ticker_home:
                        ticker_away = [t for t in all_tickers if t != ticker_home][0]
                    elif not ticker_home and not ticker_away and len(all_tickers) >= 2:
                        ticker_away = all_tickers[0]
                        ticker_home = all_tickers[1]

                    if ticker_home and ticker_away:
                        matches.append(BoxingMatch(
                            fighter_a=away,  # Away is fighter A (listed first)
                            fighter_b=home,  # Home is fighter B
                            ticker_a=ticker_away,
                            ticker_b=ticker_home,
                            odds_a=best_away,
                            odds_b=best_home,
                            odds_draw=best_draw if best_draw > 0 else 20.0,  # Default draw odds if not available
                            bookmakers=bookmakers,
                            commence_time=odds_event["commence_time"]
                        ))
                break

    return matches


def scan() -> list[BoxingMatch]:
    """Scan for boxing markets with odds coverage."""
    kalshi = get_kalshi_markets()
    odds = get_odds_api_events()
    return match_events(kalshi, odds)


def print_dashboard_ready():
    """Print matches formatted for dashboard input."""
    matches = scan()

    if not matches:
        print("No matches with odds coverage found.")
        return

    print(f"Found {len(matches)} matches with odds coverage:\n")
    print("=" * 90)

    for m in matches:
        print(m)
        print()


if __name__ == "__main__":
    print_dashboard_ready()
