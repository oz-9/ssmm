from theocalculator import calculate_theo_td_price
from kalshiorderbook import get_player_td_orderbook
import math

home_team = "DENVER_BRONCOS_NFL"
away_team = "BUFFALO_BILLS_NFL"
player_id = "BO_NIX_1_NFL"
player_name = "Bo Nix"

def classification(home_team, away_team, player_id, player_name, contracts=100):

    theo_price_yes = calculate_theo_td_price(home_team, away_team, player_id)
    theo_price_no = 1 - theo_price_yes
    print(f"theo for {player_name} is {theo_price_yes:.3f} for yes & {theo_price_no:.3f} for no")

    orderbook = get_player_td_orderbook(player_name)

    ticker, market_data = next(iter(orderbook.items()))
    yes_bids = market_data.get('yes_bids', [])
    no_bids = market_data.get('no_bids', [])

    # Top bids in dollars
    best_yes_bid = round(max([b[0] for b in yes_bids], default=0) / 100, 2)
    best_no_bid = round(max([b[0] for b in no_bids], default=0) / 100, 2)

    best_no_ask = round(1 - best_yes_bid, 2)
    best_yes_ask = round(1 - best_no_bid, 2)

    print(f"best yes ask is {best_yes_ask} and best no ask is {best_no_ask}")



    print(f"functional yes ask is {effective_yes_ask_per_contracts} and best no ask is {effective_no_ask_per_contracts}")

    if best_no_ask >= theo_price_no and best_yes_ask >= theo_price_yes:
        status = "marketmakeable"
    else:
        print("wtf")
        status = "fail"

    return status

classification(home_team, away_team, player_id, player_name)