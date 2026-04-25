"""
Central configuration. Reads from DB where possible, falls back to env/defaults.
"""
import os
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')

# Risk and portfolio settings
RISK_LEVEL = os.getenv('RISK_LEVEL', 'medium')
PORTFOLIO_CURRENCY = os.getenv('PORTFOLIO_CURRENCY', 'EUR')
HOME_COUNTRY = os.getenv('HOME_COUNTRY', 'FI')
MAX_SECTOR_EXPOSURE = 0.25
MAX_SINGLE_STOCK = 0.15
MAX_ALPHA_VANTAGE_CALLS = int(os.getenv('MAX_ALPHA_VANTAGE_CALLS', 22))
MAX_STOCKS_DEEP_ANALYSIS = 5
MIN_GDELT_ARTICLES = 5
MIN_MOMENTUM_SIGNAL = 5.0
POLYMARKET_MIN_PROBABILITY = 0.60

# Default watchlist (used if DB watchlist is empty)
WATCHLIST = {
    'AAPL': 'Apple',
    'NVDA': 'NVIDIA',
    'TSM': 'Taiwan Semiconductor TSMC',
    'XOM': 'ExxonMobil',
    'MSFT': 'Microsoft',
    'META': 'Meta Facebook',
    'AMZN': 'Amazon',
    'TSLA': 'Tesla',
}


def get_watchlist():
    """Read watchlist from DB, fall back to WATCHLIST constant."""
    try:
        import sys
        sys.path.insert(0, '/home/ubuntu/advisor')
        from db.database import query
        rows = query("SELECT ticker, company_name FROM watchlist WHERE active=TRUE")
        if rows:
            return {r['ticker']: r['company_name'] or r['ticker'] for r in rows}
    except Exception:
        pass
    return WATCHLIST


def get_setting(key, default=None):
    """Read a user setting from DB."""
    try:
        import sys
        sys.path.insert(0, '/home/ubuntu/advisor')
        from db.database import query
        rows = query("SELECT value FROM user_settings WHERE key=%s", (key,))
        if rows and rows[0]['value']:
            return rows[0]['value']
    except Exception:
        pass
    return default
