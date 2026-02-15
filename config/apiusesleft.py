import requests
from config import SPORTSGAMEODDS_API_KEY

def get_account_usage(api_key):
    """
    Calls the SportsGameOdds /v2/account/usage endpoint and prints usage info.

    Args:
      api_key (str): Your SportsGameOdds API key.
    """
    url = "https://api.sportsgameodds.com/v2/account/usage"
    headers = {
        "X-API-Key": api_key
    }

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error calling usage endpoint: {e}")
        return

    data = response.json()

    # Print the raw JSON for reference
    print("Raw usage response:")
    print(data)

    # Optionally parse and display specific fields
    if "data" in data:
        usage = data["data"]
        print("\nAccount Usage:")
        for key, value in usage.items():
            print(f"{key}: {value}")
    else:
        print("Unexpected response format, no 'data' found.")

# Example usage
API_KEY = SPORTSGAMEODDS_API_KEY
get_account_usage(API_KEY)
