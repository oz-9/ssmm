import requests
from config.config import SPORTSGAMEODDS_API_KEY

API_URL = "https://api.sportsgameodds.com/v2/events"

def get_player_td_odds(game_home_team, game_away_team, player_id):
    """
    Fetch anytime TD odds for a given player in a specific NFL game.

    Returns a dict of bookmaker -> odds, each bookmaker only once.
    """
    odd_id = f"touchdowns-{player_id}-game-yn-yes"

    params = {
        "leagueID": "NFL",
        "oddsAvailable": "true",
        "oddID": odd_id,
        "limit": 50,
        "includeAltLines": "true",
        "apiKey": SPORTSGAMEODDS_API_KEY
    }

    response = requests.get(API_URL, params=params)
    if response.status_code != 200:
        print("Error fetching data:", response.status_code, response.text)
        return None

    events = response.json().get("data", [])
    for event in events:
        home_team = event.get("teams", {}).get("home", {}).get("teamID")
        away_team = event.get("teams", {}).get("away", {}).get("teamID")
        if home_team == game_home_team and away_team == game_away_team:
            odds_info = event.get("odds", {}).get(odd_id, {}).get("byBookmaker", {})
            # Deduplicate: only one entry per bookmaker
            result = {}
            for bookmaker, bookmaker_data in odds_info.items():
                if bookmaker not in result:
                    result[bookmaker] = bookmaker_data.get("odds")
            return result  # Return immediately after first match

    return None
