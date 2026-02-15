"""
Check what sports/markets are available on The Odds API for our Kalshi opportunities.
"""

import requests
from config.config import ODDSAPI_API_KEY

BASE_URL = "https://api.the-odds-api.com/v4"


def get_available_sports():
    """Get list of available sports on The Odds API."""
    url = f"{BASE_URL}/sports"
    params = {"apiKey": ODDSAPI_API_KEY}

    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        print(f"Error: {resp.status_code} - {resp.text}")
        return []

    # Show remaining quota
    print(f"API Quota - Remaining: {resp.headers.get('x-requests-remaining')}, Used: {resp.headers.get('x-requests-used')}")

    return resp.json()


def get_events(sport_key: str):
    """Get upcoming events for a sport (free endpoint)."""
    url = f"{BASE_URL}/sports/{sport_key}/events"
    params = {"apiKey": ODDSAPI_API_KEY}

    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        print(f"Error: {resp.status_code} - {resp.text}")
        return []

    return resp.json()


def get_odds(sport_key: str, markets: str = "h2h"):
    """Get odds for a sport (costs credits)."""
    url = f"{BASE_URL}/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDSAPI_API_KEY,
        "regions": "us",
        "markets": markets,
        "oddsFormat": "american"
    }

    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        print(f"Error: {resp.status_code} - {resp.text}")
        return []

    print(f"API Quota - Remaining: {resp.headers.get('x-requests-remaining')}, Used: {resp.headers.get('x-requests-used')}")
    return resp.json()


if __name__ == "__main__":
    print("=" * 70)
    print("THE ODDS API - AVAILABLE SPORTS")
    print("=" * 70)

    sports = get_available_sports()

    # Filter for active sports
    active_sports = [s for s in sports if s.get("active", False)]

    print(f"\nActive sports ({len(active_sports)}):\n")

    # Group by group
    groups = {}
    for sport in active_sports:
        group = sport.get("group", "Other")
        if group not in groups:
            groups[group] = []
        groups[group].append(sport)

    for group, sports_list in sorted(groups.items()):
        print(f"\n{group}:")
        for sport in sports_list:
            print(f"  {sport['key']:40} - {sport['title']}")

    # Check college basketball specifically
    print("\n" + "=" * 70)
    print("COLLEGE BASKETBALL EVENTS")
    print("=" * 70)

    ncaab_events = get_events("basketball_ncaab")

    if ncaab_events:
        print(f"\nFound {len(ncaab_events)} upcoming NCAAB events:\n")

        # Show first 20
        for event in ncaab_events[:20]:
            home = event.get("home_team", "?")
            away = event.get("away_team", "?")
            time = event.get("commence_time", "?")
            print(f"  {away:25} @ {home:25} | {time}")

        if len(ncaab_events) > 20:
            print(f"  ... and {len(ncaab_events) - 20} more")
    else:
        print("No NCAAB events found")

    # Check soccer
    print("\n" + "=" * 70)
    print("CHAMPIONS LEAGUE EVENTS")
    print("=" * 70)

    ucl_events = get_events("soccer_uefa_champs_league")

    if ucl_events:
        print(f"\nFound {len(ucl_events)} upcoming UCL events:\n")
        for event in ucl_events[:15]:
            home = event.get("home_team", "?")
            away = event.get("away_team", "?")
            time = event.get("commence_time", "?")
            print(f"  {away:25} @ {home:25} | {time}")
    else:
        print("No UCL events found")

    # Check FA Cup
    print("\n" + "=" * 70)
    print("FA CUP EVENTS")
    print("=" * 70)

    facup_events = get_events("soccer_fa_cup")

    if facup_events:
        print(f"\nFound {len(facup_events)} upcoming FA Cup events:\n")
        for event in facup_events[:15]:
            home = event.get("home_team", "?")
            away = event.get("away_team", "?")
            time = event.get("commence_time", "?")
            print(f"  {away:25} @ {home:25} | {time}")
    else:
        print("No FA Cup events found")
