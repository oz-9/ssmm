"""
NCAA Lacrosse market scanner - matches Kalshi markets with Odds API odds.
"""

import requests
from dataclasses import dataclass
from typing import Optional
from config.config import ODDSAPI_API_KEY

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# School name mappings (Odds API mascot -> Kalshi short name)
SCHOOL_MAP = {
    "red foxes": "marist",
    "bulldogs": "yale",
    "raiders": "colgate",
    "crimson": "harvard",
    "keydets": "vmi",
    "midshipmen": "navy",
    "fighting irish": "notre dame",
    "knights": "bellarmine",
    "stags": "fairfield",
    "pioneers": "sacred heart",
    "dragons": "drexel",
    "leopards": "lafayette",
    "red storm": "st. john's",
    "bobcats": "quinnipiac",
    "bearcats": "binghamton",
    "saints": "siena",
    "seahawks": "wagner",
    "hawks": "monmouth",
    "colonials": "robert morris",
    "bonnies": "st. bonaventure",
    "gaels": "iona",
    "bison": "bucknell",
    "royals": "queens",
    "mountaineers": "mt. st. mary's",
    "golden griffins": "canisius",
    "lakers": "mercyhurst",
    "quakers": "pennsylvania",
    "great danes": "albany",
    "blue jays": "johns hopkins",
    "tar heels": "north carolina",
    "tigers": "towson",
    "hawks": "saint joseph's",
    "greyhounds": "loyola",
    "retrievers": "umbc",
}


def normalize_team(name: str) -> str:
    """Convert Odds API team name to Kalshi format."""
    name_lower = name.lower()

    # Try mascot mapping
    for mascot, school in SCHOOL_MAP.items():
        if mascot in name_lower:
            return school

    # Fall back to first word (school name)
    return name_lower.split()[0]


@dataclass
class LacrosseMatch:
    """A lacrosse match with Kalshi tickers and odds."""
    home_team: str
    away_team: str
    ticker_home: str
    ticker_away: str
    odds_home: float  # decimal, from Odds API
    odds_away: float
    bookmakers: list[str]
    commence_time: str

    @property
    def theo_home(self) -> int:
        """No-vig theo for home team (cents)."""
        imp_h = 1 / self.odds_home
        imp_a = 1 / self.odds_away
        return int(round(imp_h / (imp_h + imp_a) * 100))

    @property
    def theo_away(self) -> int:
        """No-vig theo for away team (cents)."""
        return 100 - self.theo_home

    @property
    def fair_odds_home(self) -> float:
        """Fair decimal odds for home (no vig)."""
        return 100 / self.theo_home

    @property
    def fair_odds_away(self) -> float:
        """Fair decimal odds for away (no vig)."""
        return 100 / self.theo_away

    def __str__(self):
        return (
            f"{self.away_team} @ {self.home_team}\n"
            f"  Time: {self.commence_time[:16]}\n"
            f"  Raw odds: {self.odds_away:.2f} / {self.odds_home:.2f} ({', '.join(self.bookmakers)})\n"
            f"  Theo: {self.theo_away}c / {self.theo_home}c\n"
            f"  Ticker A: {self.ticker_away}\n"
            f"  Ticker B: {self.ticker_home}\n"
            f"  Dashboard odds: {self.fair_odds_away:.2f} / {self.fair_odds_home:.2f}"
        )


def get_kalshi_markets() -> dict:
    """Get all Kalshi lacrosse markets, grouped by event."""
    r = requests.get(f"{KALSHI_BASE}/markets",
        params={"series_ticker": "KXNCAAMLAXGAME", "limit": 200})
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
    """Get NCAA lacrosse odds from The Odds API."""
    r = requests.get(f"{ODDS_API_BASE}/sports/lacrosse_ncaa/odds",
        params={
            "apiKey": ODDSAPI_API_KEY,
            "regions": "us,us2",
            "markets": "h2h",
            "oddsFormat": "decimal"
        })
    return r.json()


def match_events(kalshi_events: dict, odds_events: list) -> list[LacrosseMatch]:
    """Match Kalshi markets with Odds API events."""
    matches = []

    for odds_event in odds_events:
        home = odds_event["home_team"]
        away = odds_event["away_team"]
        home_norm = normalize_team(home)
        away_norm = normalize_team(away)

        # Find matching Kalshi event
        for base, data in kalshi_events.items():
            title_lower = data["title"].lower()

            # Check if both teams appear
            if home_norm in title_lower and away_norm in title_lower:
                # Get best odds
                best_home = best_away = 0
                bookmakers = []

                for bm in odds_event.get("bookmakers", []):
                    for outcome in bm["markets"][0]["outcomes"]:
                        if outcome["name"] == home and outcome["price"] > best_home:
                            best_home = outcome["price"]
                        if outcome["name"] == away and outcome["price"] > best_away:
                            best_away = outcome["price"]
                    bookmakers.append(bm["key"])

                if best_home > 0 and best_away > 0:
                    # Figure out which ticker is which team
                    tickers = data["tickers"]

                    # Match ticker codes to teams by checking if code appears in normalized name
                    # or if first letters of name appear in code
                    ticker_home = ticker_away = None
                    for code, ticker in tickers.items():
                        code_lower = code.lower()
                        # Get first word/letters of each team for matching
                        home_first = home_norm.replace(".", "").replace(" ", "")[:4]
                        away_first = away_norm.replace(".", "").replace(" ", "")[:4]

                        # Check for home team match
                        if code_lower in home_norm.replace(".", "").replace(" ", "") or \
                           home_first.startswith(code_lower) or code_lower.startswith(home_first[:3]):
                            ticker_home = ticker
                        # Check for away team match
                        elif code_lower in away_norm.replace(".", "").replace(" ", "") or \
                             away_first.startswith(code_lower) or code_lower.startswith(away_first[:3]):
                            ticker_away = ticker

                    # Fallback: assign unmatched ticker to missing slot
                    all_tickers = list(tickers.values())
                    if not ticker_home and ticker_away:
                        ticker_home = [t for t in all_tickers if t != ticker_away][0]
                    elif not ticker_away and ticker_home:
                        ticker_away = [t for t in all_tickers if t != ticker_home][0]
                    elif not ticker_home and not ticker_away:
                        # Complete fallback - just assign by order
                        ticker_away = all_tickers[0]
                        ticker_home = all_tickers[1] if len(all_tickers) > 1 else all_tickers[0]

                    matches.append(LacrosseMatch(
                        home_team=home,
                        away_team=away,
                        ticker_home=ticker_home,
                        ticker_away=ticker_away,
                        odds_home=best_home,
                        odds_away=best_away,
                        bookmakers=bookmakers,
                        commence_time=odds_event["commence_time"]
                    ))
                break

    return matches


def scan() -> list[LacrosseMatch]:
    """Scan for lacrosse markets with odds coverage."""
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
