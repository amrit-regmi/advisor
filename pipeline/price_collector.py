import yfinance as yf
import sys
import time
from datetime import date, timedelta
sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import execute, query, log, track_api_call

def fetch_and_store_prices(ticker, days_back=365):
    try:
        end = date.today()
        start = end - timedelta(days=days_back)

        track_api_call('yfinance')
        stock = yf.Ticker(ticker)
        hist = stock.history(start=start, end=end)

        if hist.empty:
            log('WARNING', 'prices', f'No price data for {ticker}')
            return 0

        count = 0
        for idx, row in hist.iterrows():
            execute("""
                INSERT INTO prices (ticker, date, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, date) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """, (
                ticker,
                idx.date(),
                float(row['Open']),
                float(row['High']),
                float(row['Low']),
                float(row['Close']),
                int(row['Volume'])
            ))
            count += 1

        latest_price = float(hist['Close'].iloc[-1])
        log('INFO', 'prices', f'{ticker}: {count} days stored, latest close ${latest_price:.2f}')
        return count

    except Exception as e:
        log('ERROR', 'prices', f'Error fetching {ticker}: {e}')
        return 0

def get_latest_price(ticker):
    result = query("""
        SELECT close, date FROM prices
        WHERE ticker = %s
        ORDER BY date DESC LIMIT 1
    """, (ticker,))
    if result:
        return float(result[0]['close']), result[0]['date']
    return None, None

def calculate_momentum(ticker, days=30):
    result = query("""
        SELECT close, date FROM prices
        WHERE ticker = %s
        ORDER BY date DESC
        LIMIT %s
    """, (ticker, days))

    if len(result) < 2:
        return 0

    latest = float(result[0]['close'])
    oldest = float(result[-1]['close'])
    return round(((latest - oldest) / oldest) * 100, 2)

def run(watchlist):
    log('INFO', 'prices', f'Starting price collection for {len(watchlist)} tickers')

    today = date.today()
    skipped = 0

    for ticker in watchlist:
        existing = query(
            "SELECT COUNT(*) as cnt, MAX(date) as latest FROM prices WHERE ticker = %s",
            (ticker,)
        )
        cnt = existing[0]['cnt'] if existing else 0
        latest = existing[0]['latest'] if existing else None

        # Skip if we already have today's price (market may still be open, but good enough)
        if latest and latest >= today:
            skipped += 1
            continue

        days_back = 1095 if cnt == 0 else 7  # 3 years on first fetch, else last 7 days
        fetch_and_store_prices(ticker, days_back)
        time.sleep(0.5)

    log('INFO', 'prices', f'Price collection complete ({len(watchlist) - skipped} fetched, {skipped} already current)')

if __name__ == '__main__':
    watchlist = ['AAPL', 'NVDA', 'TSM', 'XOM', 'MSFT', 'META', 'AMZN', 'TSLA']
    run(watchlist)
