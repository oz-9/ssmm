import requests

def get_player_td_orderbook(player_name):
    series_ticker = "KXNFLANYTD"
    markets_url = f"https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={series_ticker}&status=open"

    markets_response = requests.get(markets_url)
    markets_data = markets_response.json()
    td_markets = markets_data.get("markets", [])

    player_markets = [
        market for market in td_markets
        if player_name.lower() in market.get("title", "").lower()
    ]

    if not player_markets:
        print(f"No active TD markets found for {player_name}.")
        return None

    result = {}

    for market in player_markets:
        ticker = market['ticker']
        orderbook_url = f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook"
        orderbook_response = requests.get(orderbook_url)

        try:
            orderbook_data = orderbook_response.json()
        except ValueError:
            print(f"Orderbook response not valid JSON for {ticker}")
            continue

        yes_bids = sorted(orderbook_data['orderbook'].get('yes', []), key=lambda x: x[0], reverse=True)[:5]
        no_bids = sorted(orderbook_data['orderbook'].get('no', []), key=lambda x: x[0], reverse=True)[:5]


        # Store in dict for programmatic use
        result[ticker] = {
            "title": market.get("title", "(no title)"),
            "yes_bids": yes_bids,
            "no_bids": no_bids
        }

    return result
