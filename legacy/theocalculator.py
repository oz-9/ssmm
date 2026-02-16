from getoddsfrombook import get_player_td_odds

def calculate_theo_td_price(home_team, away_team, player_id):
    # Step 1: get odds from all sportsbooks
    odds = get_player_td_odds(home_team, away_team, player_id)
    if not odds:
        return None

    # Step 2: define weights for each bookmaker
    weights = {
        "betmgm": 0.4,
        "bovada": 0.3,
        "caesars": 0.5,
        "draftkings": 0.9,
        "espnbet": 0.3,
        "fanduel": 1.0
    }

    # Step 3: helper to convert American odds to probability
    def american_to_prob(odd_str):
        odd_str = odd_str.strip()
        try:
            odd = int(odd_str)  # preserves sign
        except ValueError:
            return None
        if odd > 0:
            return 100 / (odd + 100)
        else:
            return -odd / (-odd + 100)

    # Step 4: compute weighted implied probability
    weighted_sum = 0
    total_weight = 0
    for bookmaker, odd_str in odds.items():
        prob = american_to_prob(odd_str)
        if prob is None:
            continue
        weight = weights.get(bookmaker.lower(), 0.5)
        weighted_sum += prob * weight
        total_weight += weight

    if total_weight == 0:
        return None

    weighted_implied = weighted_sum / total_weight

    # Step 5: remove rough vig factor (optional)
    vig_factor = 0.05  # adjust if you want
    no_vig_prob = weighted_implied / (1 + vig_factor)

    # Step 6: clamp to [0,1] and return
    return max(min(no_vig_prob, 1.0), 0.0)
