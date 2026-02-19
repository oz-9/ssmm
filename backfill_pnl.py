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


def extract_match_base(ticker):
    """Extract match base from ticker (e.g., KXVALORANTGAME-26FEB14DFMPR from KXVALORANTGAME-26FEB14DFMPR-DFM)."""
    parts = ticker.rsplit('-', 1)
    return parts[0] if len(parts) == 2 else ticker


def extract_team(ticker):
    """Extract team from ticker (e.g., DFM from KXVALORANTGAME-26FEB14DFMPR-DFM)."""
    parts = ticker.rsplit('-', 1)
    return parts[1] if len(parts) == 2 else ticker


def main():
    print("Initializing P&L database...")
    pnl_db.init_db()

    print("Connecting to Kalshi...")
    client = KalshiClient(KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH)

    print("Fetching all fills...")
    all_fills = get_all_fills(client)
    print(f"Total fills: {len(all_fills)}")

    # Group fills by match base
    fills_by_match = defaultdict(list)
    for f in all_fills:
        match_base = extract_match_base(f['ticker'])
        fills_by_match[match_base].append(f)

    print(f"Unique match bases: {len(fills_by_match)}")

    # Process each match
    matches_created = 0
    fills_inserted = 0

    for match_base, fills in sorted(fills_by_match.items()):
        # Get unique tickers for this match
        tickers = sorted(set(f['ticker'] for f in fills))

        if len(tickers) == 1:
            # Only one side traded - create match with inferred other ticker
            ticker_a = tickers[0]
            team_a = extract_team(ticker_a)
            # Can't infer ticker_b, use placeholder
            ticker_b = f"{match_base}-???"
            match_id = f"{team_a}v???"
        elif len(tickers) == 2:
            ticker_a, ticker_b = tickers
            team_a = extract_team(ticker_a)
            team_b = extract_team(ticker_b)
            match_id = f"{team_a}v{team_b}"
        else:
            # More than 2 tickers? Shouldn't happen, skip
            print(f"  Skipping {match_base}: {len(tickers)} tickers")
            continue

        # Create match entry
        pnl_db.upsert_match(
            match_id=match_id,
            ticker_a=ticker_a,
            ticker_b=ticker_b,
            theo_a=50,
            theo_b=50,
        )
        matches_created += 1

        # Insert all fills for this match
        for f in fills:
            pnl_db.insert_fill(
                fill_id=f['fill_id'],
                ticker=f['ticker'],
                side=f['side'],
                action=f['action'],
                price=f['yes_price'] if f['side'] == 'yes' else f['no_price'],
                count=f['count'],
                is_taker=f.get('is_taker', False),
                fee_cost=int(float(f.get('fee_cost', '0')) * 100),
                created_time=f['created_time'],
                match_id=match_id,
            )
            fills_inserted += 1

    print(f"\nBackfill complete!")
    print(f"  Matches created: {matches_created}")
    print(f"  Fills inserted: {fills_inserted}")

    # Show summary by category
    print("\nP&L by category:")
    with pnl_db.get_db() as conn:
        matches = conn.execute("SELECT * FROM pnl_matches").fetchall()

    by_cat = defaultdict(lambda: {"arb": 0, "pairs": 0, "leftover": 0})
    for m in matches:
        ticker = m['ticker_a']
        if 'VALORANT' in ticker:
            cat = 'Valorant'
        elif 'CS2' in ticker:
            cat = 'CS2'
        elif 'DOTA' in ticker:
            cat = 'Dota'
        elif 'LOL' in ticker:
            cat = 'LoL'
        elif 'LAX' in ticker:
            cat = 'Lacrosse'
        elif 'BOXING' in ticker:
            cat = 'Boxing'
        elif 'NBA' in ticker:
            cat = 'NBA'
        else:
            cat = 'Other'

        pnl = pnl_db.calculate_match_pnl(m['id'], m['theo_a'] or 50, m['theo_b'] or 50)
        if 'error' not in pnl:
            by_cat[cat]["arb"] += pnl['arb_profit']
            by_cat[cat]["pairs"] += pnl['arb_pairs']
            by_cat[cat]["leftover"] += pnl['leftover_a'] + pnl['leftover_b']

    for cat, data in sorted(by_cat.items(), key=lambda x: -x[1]["arb"]):
        print(f"  {cat}: ${data['arb']/100:.2f} arb ({data['pairs']} pairs, {data['leftover']} leftover)")


if __name__ == "__main__":
    main()
