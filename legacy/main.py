from theocalculator import calculate_theo_td_price
from kalshiorderbook import get_player_td_orderbook

# Example call
home_team = "DENVER_BRONCOS_NFL"
away_team = "BUFFALO_BILLS_NFL"
player_id = "JAMES_COOK_1_NFL"
player_name = "James Cook"

odds = calculate_theo_td_price(home_team, away_team, player_id)
orderbook = get_player_td_orderbook(player_name)

# Then print selectively
print("Orderbook:", orderbook)
print("Odds:", odds)