import os
import sys
sys.path.insert(0, '/home/ubuntu/advisor')
from fredapi import Fred
from datetime import date, timedelta
from db.database import execute, query, log
from dotenv import load_dotenv

load_dotenv('/home/ubuntu/advisor/.env')

FRED_SERIES = {
    'DFF':    'Fed funds rate',
    'T10Y2Y': 'Yield curve 10y-2y spread',
    'CPIAUCSL': 'US CPI inflation',
    'UNRATE': 'US unemployment rate',
    'DGS10':  'US 10yr treasury yield',
    'VIXCLS': 'VIX volatility index',
    'DEXUSEU': 'EUR/USD exchange rate',
    'DCOILWTICO': 'WTI crude oil price',
    'BAMLH0A0HYM2': 'High yield credit spread',
    'T5YIFR': '5yr inflation expectations',
}

def fetch_and_store_series(fred, series_id, series_name):
    try:
        end = date.today()
        start = end - timedelta(days=30)
        data = fred.get_series(
            series_id,
            observation_start=start.strftime('%Y-%m-%d'),
            observation_end=end.strftime('%Y-%m-%d')
        )

        count = 0
        for obs_date, value in data.items():
            if not str(value) == 'nan':
                execute("""
                    INSERT INTO macro_data (series_id, date, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (series_id, date)
                    DO UPDATE SET value = EXCLUDED.value
                """, (series_id, obs_date.date(), float(value)))
                count += 1

        latest = data.dropna().iloc[-1] if len(data.dropna()) > 0 else None
        log('INFO', 'fred',
            f'{series_id} ({series_name}): {count} observations, latest={latest:.4f}' 
            if latest else f'{series_id}: no data')
        return True

    except Exception as e:
        log('ERROR', 'fred', f'Error fetching {series_id}: {e}')
        return False

def get_latest_macro():
    results = {}
    for series_id in FRED_SERIES:
        data = query("""
            SELECT value, date FROM macro_data
            WHERE series_id = %s
            ORDER BY date DESC LIMIT 1
        """, (series_id,))
        if data:
            results[series_id] = {
                'value': float(data[0]['value']),
                'date': data[0]['date'],
                'name': FRED_SERIES[series_id]
            }
    return results

def run():
    log('INFO', 'fred', 'Starting FRED macro data collection')
    api_key = os.getenv('FRED_API_KEY')

    if not api_key:
        log('ERROR', 'fred', 'No FRED API key found in .env')
        return

    fred = Fred(api_key=api_key)
    success = 0

    for series_id, series_name in FRED_SERIES.items():
        if fetch_and_store_series(fred, series_id, series_name):
            success += 1

    log('INFO', 'fred', f'FRED collection complete — {success}/{len(FRED_SERIES)} series updated')

if __name__ == '__main__':
    run()
