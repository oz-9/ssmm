"""
Backfill P&L database from Kalshi fill history.
"""

from mm import KalshiClient
from config.config import KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH
import pnl_db
from collections import defaultdict

def get_all_fills(client):
    """Fetch all fills from Kalshi with pagination."""
    all_fills = []
    cursor = None
    while True:
        path = '/portfolio/fills?limit=100'
        if cursor:
            path += f'&cursor={cursor}'
        resp = client.get(path)
        data = resp.json()
        fills = data.get('fills', [])
        all_fills.extend(fills)
        cursor = data.get('cursor')
        if not cursor or not fills:
            break
        print(f'  Fetched {len(all_fills)} fills...')
    return all_fills


def extract_match_id(ticker):
    """Extract match base from ticker (e.g., KXVALORANTGAME-26FEB14DFMPR from KXVALORANTGAME-26FEB14DFMPR-DFM)."""
    parts = ticker.rsplit('-', 1)
    return parts[0] if len(parts) == 2 else ticker


def get_ticker_pair(fills_by_ticker, match_base):
    """Get the two tickers for a match."""
    tickers = [t for t in fills_by_ticker.keys() if t.startswith(match_base + '-')]
    if len(tickers) == 2:
        return sorted(tickers)
    return None


def main():
    print("Initializing P&L database...")
    pnl_db.init_db()

    print("Connecting to Kalshi...")
    client = KalshiClient(KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH)

    print("Fetching all fills...")
    all_fills = get_all_fills(client)
    print(f"Total fills: {len(all_fills)}")

    # Group fills by ticker
    fills_by_ticker = defaultdict(list)
    for f in all_fills:
        fills_by_ticker[f['ticker']].append(f)

    print(f"Unique tickers: {len(fills_by_ticker)}")

    # Group by match base
    match_bases = set()
    for ticker in fills_by_ticker.keys():
        match_bases.add(extract_match_id(ticker))

    print(f"Unique matches: {len(match_bases)}")

    # Process each match
    matches_created = 0
    fills_inserted = 0

    for match_base in sorted(match_bases):
        ticker_pair = get_ticker_pair(fills_by_ticker, match_base)
        if not ticker_pair:
            continue

        ticker_a, ticker_b = ticker_pair
        match_id = f"{ticker_a.split('-')[-1]}v{ticker_b.split('-')[-1]}"

        # Get theo from market (default 50/50)
        theo_a, theo_b = 50, 50

        # Create match entry
        pnl_db.upsert_match(
            match_id=match_id,
            ticker_a=ticker_a,
            ticker_b=ticker_b,
            theo_a=theo_a,
            theo_b=theo_b,
        )
        matches_created += 1

        # Insert fills for both tickers
        for ticker in [ticker_a, ticker_b]:
            for f in fills_by_ticker.get(ticker, []):
                pnl_db.insert_fill(
                    fill_id=f['fill_id'],
                    ticker=f['ticker'],
                    side=f['side'],
                    action=f['action'],
                    price=f['yes_price'] if f['side'] == 'yes' else f['no_price'],
                    count=f['count'],
                    is_taker=f.get('is_taker', False),
                    fee_cost=int(float(f.get('fee_cost', '0')) * 100),  # Convert to cents
                    created_time=f['created_time'],
                    match_id=match_id,
                )
                fills_inserted += 1

    print(f"\nBackfill complete!")
    print(f"  Matches created: {matches_created}")
    print(f"  Fills inserted: {fills_inserted}")

    # Show P&L for some matches
    print("\nSample P&L calculations:")
    for match_base in ['KXVALORANTGAME-26FEB14DFMPR', 'KXVALORANTGAME-26FEB15RRQPR']:
        ticker_pair = get_ticker_pair(fills_by_ticker, match_base)
        if ticker_pair:
            ticker_a, ticker_b = ticker_pair
            match_id = f"{ticker_a.split('-')[-1]}v{ticker_b.split('-')[-1]}"
            pnl = pnl_db.calculate_match_pnl(match_id, 50, 50)
            if 'error' not in pnl:
                print(f"  {match_id}:")
                print(f"    Arb profit: ${pnl['arb_profit']/100:.2f}")
                print(f"    Pairs: {pnl['arb_pairs']}")
                print(f"    Leftover: {pnl['leftover_a']} A / {pnl['leftover_b']} B")


if __name__ == "__main__":
    main()
