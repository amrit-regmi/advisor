"""
Universe loader — uses adanos-software free-ticker-database (GitHub).
61k+ tickers across 67 exchanges. Falls back to cached data on failure.
Refresh: monthly for database download, weekly for yfinance validation.
"""
import io
import sys
import time
import json
import requests
import pandas as pd
from datetime import date
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import execute, query, log

CACHE_DIR = Path('/home/ubuntu/advisor/data/universe_cache')
DB_URL = 'https://raw.githubusercontent.com/adanos-software/free-ticker-database/main/data/tickers.csv'

# Target exchanges: exchange_code -> (yf_suffix, country_code)
TARGET_EXCHANGES = {
    'NASDAQ': ('', 'US'),
    'NYSE': ('', 'US'),
    'AMEX': ('', 'US'),
    'NYSE MKT': ('', 'US'),
    'NYSE ARCA': ('', 'US'),
    'XETRA': ('.DE', 'DE'),
    'FSX': ('.F', 'DE'),
    'LSE': ('.L', 'GB'),
    'LON': ('.L', 'GB'),
    'SIX': ('.SW', 'CH'),
    'HEL': ('.HE', 'FI'),
    'XHEL': ('.HE', 'FI'),
    'STO': ('.ST', 'SE'),
    'XSTO': ('.ST', 'SE'),
    'CPH': ('.CO', 'DK'),
    'XCPH': ('.CO', 'DK'),
    'FNSE': ('.ST', 'SE'),
    'FNFI': ('.HE', 'FI'),
    'FNDK': ('.CO', 'DK'),
}

YF_BATCH_SIZE = 50
MONTHLY_REFRESH_DAYS = 30
WEEKLY_VALIDATE_DAYS = 7


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def _needs_refresh(path: Path, days: int) -> bool:
    if not path.exists():
        return True
    age = (date.today() - date.fromtimestamp(path.stat().st_mtime)).days
    return age >= days


def _download_ticker_db() -> pd.DataFrame:
    cache = _cache_path('tickers_raw.csv')
    if not _needs_refresh(cache, MONTHLY_REFRESH_DAYS):
        log('INFO', 'universe', 'Using cached ticker database (< 30 days old)')
        return pd.read_csv(cache, low_memory=False)

    log('INFO', 'universe', 'Downloading free-ticker-database from GitHub...')
    try:
        resp = requests.get(DB_URL, timeout=120, headers={'User-Agent': 'advisor-bot/1.0'})
        resp.raise_for_status()
        cache.write_bytes(resp.content)
        df = pd.read_csv(io.BytesIO(resp.content), low_memory=False)
        log('INFO', 'universe', f'Downloaded {len(df):,} tickers')
        return df
    except Exception as e:
        log('ERROR', 'universe', f'Download failed: {e}')
        if cache.exists():
            log('WARNING', 'universe', 'Using stale cache as fallback')
            return pd.read_csv(cache, low_memory=False)
        raise


def _find_col(df: pd.DataFrame, candidates: list):
    for c in candidates:
        if c in df.columns:
            return c
    for c in candidates:
        for col in df.columns:
            if isinstance(col, str) and c.lower() in col.lower():
                return col
    return None


def _filter_target_exchanges(df: pd.DataFrame) -> pd.DataFrame:
    exc_col = _find_col(df, ['exchange', 'Exchange', 'EXCHANGE', 'mic', 'MIC'])
    ticker_col = _find_col(df, ['ticker', 'Ticker', 'symbol', 'Symbol'])
    name_col = _find_col(df, ['name', 'Name', 'company', 'Company', 'company_name'])
    sector_col = _find_col(df, ['sector', 'Sector', 'industry', 'Industry'])
    country_col = _find_col(df, ['country', 'Country', 'country_code'])

    if not exc_col or not ticker_col:
        log('ERROR', 'universe', f'Missing required columns. Found: {list(df.columns)}')
        return pd.DataFrame()

    df = df.copy()
    df['_exc_norm'] = df[exc_col].astype(str).str.upper().str.strip()
    matched = df[df['_exc_norm'].isin(TARGET_EXCHANGES.keys())].copy()
    log('INFO', 'universe', f'Exchange filter: {len(matched):,} tickers from target exchanges')

    rows = []
    for _, row in matched.iterrows():
        exc = row['_exc_norm']
        suffix, country = TARGET_EXCHANGES[exc]
        raw_ticker = str(row[ticker_col]).strip()
        if not raw_ticker or raw_ticker.lower() in ('nan', ''):
            continue
        yf_ticker = raw_ticker + suffix if suffix and not raw_ticker.endswith(suffix) else raw_ticker
        rows.append({
            'ticker': yf_ticker,
            'company_name': str(row[name_col]).strip() if name_col else '',
            'exchange': exc,
            'sector': str(row[sector_col]).strip() if sector_col else '',
            'country': country,
        })

    return pd.DataFrame(rows)


def _validate_batch_yfinance(tickers: list) -> set:
    """Return set of tickers that have recent price data."""
    import yfinance as yf
    valid = set()
    try:
        data = yf.download(tickers, period='5d', progress=False, threads=True, auto_adjust=True)
        if len(tickers) == 1:
            if 'Close' in data.columns and len(data['Close'].dropna()) > 0:
                valid.add(tickers[0])
        else:
            close = data.get('Close', pd.DataFrame())
            for t in tickers:
                if t in close.columns and len(close[t].dropna()) > 0:
                    valid.add(t)
    except Exception as e:
        log('WARNING', 'universe', f'yfinance batch error: {e}')
    return valid


def _load_validated_cache() -> set:
    cache = _cache_path('validated_tickers.json')
    if not _needs_refresh(cache, WEEKLY_VALIDATE_DAYS) and cache.exists():
        data = json.loads(cache.read_text())
        return set(data.get('valid', []))
    return set()


def _save_validated_cache(valid_tickers: set):
    cache = _cache_path('validated_tickers.json')
    cache.write_text(json.dumps({'valid': list(valid_tickers), 'date': str(date.today())}))


def load_universe(validate: bool = True, max_validate: int = 3000) -> int:
    """Download, filter, validate and store universe. Returns count stored."""
    log('INFO', 'universe', 'Starting universe load')

    raw_df = _download_ticker_db()
    filtered = _filter_target_exchanges(raw_df)
    if filtered.empty:
        log('ERROR', 'universe', 'No tickers after exchange filter')
        return 0

    validated_cache = _load_validated_cache()

    if validate:
        to_validate = [t for t in filtered['ticker'].tolist() if t not in validated_cache]
        # Cap to avoid excessive API calls per run
        if len(to_validate) > max_validate:
            import random
            to_validate = random.sample(to_validate, max_validate)
        log('INFO', 'universe', f'Validating {len(to_validate):,} tickers via yfinance')

        new_valid = set()
        for i in range(0, len(to_validate), YF_BATCH_SIZE):
            batch = to_validate[i:i + YF_BATCH_SIZE]
            batch_valid = _validate_batch_yfinance(batch)
            new_valid.update(batch_valid)
            log('INFO', 'universe', f'  Batch {i//YF_BATCH_SIZE + 1}/{(len(to_validate)-1)//YF_BATCH_SIZE + 1}: {len(batch_valid)}/{len(batch)} valid')
            time.sleep(1)

        validated_cache.update(new_valid)
        _save_validated_cache(validated_cache)
        log('INFO', 'universe', f'Total validated tickers in cache: {len(validated_cache):,}')
    else:
        validated_cache = set(filtered['ticker'].tolist())

    valid_df = filtered[filtered['ticker'].isin(validated_cache)].copy()
    log('INFO', 'universe', f'{len(valid_df):,} tickers ready to store')

    stored = 0
    for _, row in valid_df.iterrows():
        try:
            execute("""
                INSERT INTO universe (ticker, company_name, exchange, sector, country, active, validated, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, true, true, NOW(), NOW())
                ON CONFLICT (ticker) DO UPDATE SET
                    company_name = EXCLUDED.company_name,
                    exchange = EXCLUDED.exchange,
                    sector = EXCLUDED.sector,
                    country = EXCLUDED.country,
                    active = true,
                    validated = true,
                    updated_at = NOW()
            """, (
                row['ticker'],
                row['company_name'][:200],
                row['exchange'],
                row['sector'][:100],
                row['country'],
            ))
            stored += 1
        except Exception as e:
            log('WARNING', 'universe', f'Store failed {row["ticker"]}: {e}')

    log('INFO', 'universe', f'Universe updated: {stored:,} tickers stored')

    summary = query("SELECT country, COUNT(*) as n FROM universe WHERE active = true GROUP BY country ORDER BY n DESC LIMIT 15")
    for r in summary:
        print(f'  {r["country"]}: {r["n"]} tickers')

    return stored


def mark_existing_validated():
    """Mark all currently active tickers in the DB as validated=TRUE (one-off backfill)."""
    execute("UPDATE universe SET validated=TRUE WHERE active=TRUE AND validated=FALSE")
    r = query("SELECT COUNT(*) AS cnt FROM universe WHERE active=TRUE AND validated=TRUE")
    log('INFO', 'universe', f'Backfill: {r[0]["cnt"]} active tickers marked as validated')
    return r[0]['cnt']


if __name__ == '__main__':
    import sys as _sys
    if '--backfill' in _sys.argv:
        n = mark_existing_validated()
        print(f'Marked {n} existing tickers as validated')
    elif '--sweep' in _sys.argv:
        # Full sweep: validate up to 10k tickers
        count = load_universe(validate=True, max_validate=10000)
        print(f'\nUniverse loaded: {count:,} validated tickers')
    else:
        count = load_universe(validate=True, max_validate=3000)
        print(f'\nUniverse loaded: {count:,} validated tickers')
