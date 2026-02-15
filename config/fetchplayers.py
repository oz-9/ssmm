import requests
from config.config import SPORTSGAMEODDS_API_KEY

url = "https://api.sportsgameodds.com/v2/events"

params = {
    "leagueID": "NFL",
    "oddsAvailable": "true",
    "limit": 10,
    "apiKey": SPORTSGAMEODDS_API_KEY
}

response = requests.get(url, params=params)

if response.status_code == 200:
    data = response.json().get("data", [])

    for event in data:
        teams = event.get("teams", {})

        # Safely get home and away team info
        home_team_info = teams.get("home", {})
        away_team_info = teams.get("away", {})

        home_team_name = home_team_info.get("name") or home_team_info.get("teamID") or "Unknown Home"
        away_team_name = away_team_info.get("name") or away_team_info.get("teamID") or "Unknown Away"

        print(f"Game: {home_team_name} vs {away_team_name}")

        # List players
        for player_id, player_info in event.get("players", {}).items():
            player_name = player_info.get("name", player_id)
            print(f"Player ID: {player_id}, Name: {player_name}")
else:
    print("Error:", response.status_code, response.text)
