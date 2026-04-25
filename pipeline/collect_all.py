"""Master data collection runner."""
import sys
sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, log
import pipeline.gdelt_collector as gdelt
import pipeline.fred_collector as fred
import pipeline.polymarket_collector as polymarket
import pipeline.price_collector as prices


def get_watchlist():
    """Read active watchlist from DB, fall back to config.py."""
    rows = query("SELECT ticker, company_name FROM watchlist WHERE active=TRUE")
    if rows:
        return {r['ticker']: r['company_name'] or r['ticker'] for r in rows}
    # Fallback
    from config import WATCHLIST
    return WATCHLIST


def get_discovery_tickers():
    """Top discovery candidates from yesterday/today that need price + GDELT data."""
    rows = query("""
        SELECT DISTINCT ON (ticker) ticker,
               u.company_name
        FROM discovery_candidates dc
        LEFT JOIN universe u USING (ticker)
        WHERE dc.date >= CURRENT_DATE - INTERVAL '1 day'
          AND dc.eligible = TRUE
        ORDER BY ticker, dc.total_score DESC
        LIMIT 50
    """)
    return {r['ticker']: r['company_name'] or r['ticker'] for r in rows}


def run():
    log('INFO', 'collector', '=== Starting daily data collection ===')
    watchlist = get_watchlist()
    log('INFO', 'collector', f'Watchlist: {list(watchlist.keys())}')

    # Merge watchlist + top discovery candidates for data collection
    disc_map = get_discovery_tickers()
    all_tickers_map = {**disc_map, **watchlist}  # watchlist takes priority on name
    all_price_tickers = list(all_tickers_map.keys())
    log('INFO', 'collector', f'Price+GDELT: {len(all_price_tickers)} tickers ({len(disc_map)} discovery, {len(watchlist)} watchlist)')

    log('INFO', 'collector', 'Step 1: Collecting prices...')
    prices.run(all_price_tickers)

    log('INFO', 'collector', 'Step 2: Collecting GDELT sentiment...')
    gdelt.run(all_tickers_map)

    log('INFO', 'collector', 'Step 3: Collecting FRED macro data...')
    fred.run()

    log('INFO', 'collector', 'Step 4: Collecting Polymarket signals...')
    polymarket.run()

    log('INFO', 'collector', 'Step 5: Pre-fetching fundamentals (holdings + watchlist + recs)...')
    try:
        from pipeline.fundamentals_cache import prefetch_fundamentals
        prefetch_fundamentals(extra_tickers=all_price_tickers)
    except Exception as e:
        log('WARNING', 'collector', f'Fundamentals prefetch failed: {e}')

    log('INFO', 'collector', '=== Daily collection complete ===')


if __name__ == '__main__':
    run()
