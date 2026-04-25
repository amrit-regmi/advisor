"""
Fundamentals cache — Yahoo Finance first, Alpha Vantage fallback.

Source priority per field:
  1. yfinance .info  — richest data, cached 12 h in data/yf_cache/
  2. yfinance fast_info — price/volume fields that work without crumb, live
  3. Alpha Vantage OVERVIEW — fallback for ratios when yf.info is unavailable,
                              cached 24 h in data/av_cache/

Pre-fetch on every pipeline run for holdings → watchlist → recs → discovery.
On-demand for any universe ticker opened in the dashboard.
"""
import os
import sys
import json
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, log

YF_CACHE_DIR = Path('/home/ubuntu/advisor/data/yf_cache')
AV_CACHE_DIR = Path('/home/ubuntu/advisor/data/av_cache')
YF_CACHE_TTL = 43200   # 12 hours — yfinance .info is rich but rate-limited
AV_CACHE_TTL = 86400   # 24 hours — AV free tier: 22 calls/day


def _safe_float(v):
    """Convert string value to float, None if missing/zero-sentinel."""
    try:
        f = float(v)
        return None if str(v).strip() in ('0', '0.0', 'None', '-', 'N/A', '') else f
    except (TypeError, ValueError):
        return None


# ── Yahoo Finance .info (primary) ─────────────────────────────────────────────

def _yf_cache_path(ticker: str) -> Path:
    YF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return YF_CACHE_DIR / f'{ticker}.json'


def get_yf_info(ticker: str) -> dict:
    """
    Fetch yfinance .info with 12-h file cache.
    Returns normalised dict on success, {} if rate-limited or ticker unknown.
    """
    cache_file = _yf_cache_path(ticker)
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < YF_CACHE_TTL:
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        if not info.get('symbol') and not info.get('shortName'):
            return {}
        result = {
            'market_cap':     info.get('marketCap'),
            'last_price':     info.get('currentPrice') or info.get('regularMarketPrice'),
            'pe_trailing':    info.get('trailingPE'),
            'pe_forward':     info.get('forwardPE'),
            'dividend_yield': info.get('trailingAnnualDividendYield') or info.get('dividendYield'),
            'beta':           info.get('beta'),
            'week52_high':    info.get('fiftyTwoWeekHigh'),
            'week52_low':     info.get('fiftyTwoWeekLow'),
            'revenue_growth': info.get('revenueGrowth'),
            'gross_margin':   info.get('grossMargins'),
            'roe':            info.get('returnOnEquity'),
            'debt_equity':    info.get('debtToEquity'),
            'analyst_target': info.get('targetMeanPrice'),
            'analyst_rating': info.get('recommendationKey', ''),
            'employees':      info.get('fullTimeEmployees'),
            'description':    (info.get('longBusinessSummary') or '')[:400],
            'sector':         info.get('sector', ''),
            'industry':       info.get('industry', ''),
            'currency':       info.get('currency', ''),
            'exchange':       info.get('exchange', ''),
            'prev_close':     info.get('regularMarketPreviousClose'),
            'avg_vol_3m':     info.get('averageVolume'),
            '_source':        'yfinance',
        }
        # Only cache if we got meaningful data
        if any(result[k] for k in ('market_cap', 'pe_trailing', 'week52_high')):
            cache_file.write_text(json.dumps(result))
        return result
    except Exception:
        return {}


# ── yfinance fast_info (price fields, no crumb needed) ───────────────────────

def get_fast_info(ticker: str) -> dict:
    """Live price/volume fields via fast_info — no cookie/crumb required."""
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        return {
            'market_cap':  getattr(fi, 'market_cap', None),
            'last_price':  getattr(fi, 'last_price', None),
            'week52_high': getattr(fi, 'year_high', None),
            'week52_low':  getattr(fi, 'year_low', None),
            'currency':    getattr(fi, 'currency', None),
            'prev_close':  getattr(fi, 'regular_market_previous_close', None),
            'avg_vol_3m':  getattr(fi, 'three_month_average_volume', None),
            '_source':     'fast_info',
        }
    except Exception:
        return {}


# ── Alpha Vantage OVERVIEW (fallback for ratios) ──────────────────────────────

def get_av_overview(ticker: str) -> dict:
    """
    Fetch Alpha Vantage OVERVIEW with 24-h file cache.
    Returns full AV dict on success, {} on failure or rate-limit.
    """
    AV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = AV_CACHE_DIR / f'{ticker}.json'

    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < AV_CACHE_TTL:
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    key = os.environ.get('ALPHA_VANTAGE_KEY', '')
    if not key:
        return {}
    try:
        import requests
        r = requests.get(
            'https://www.alphavantage.co/query',
            params={'function': 'OVERVIEW', 'symbol': ticker, 'apikey': key},
            timeout=10,
        )
        data = r.json()
        if data.get('Symbol'):
            cache_file.write_text(json.dumps(data))
            return data
    except Exception as e:
        log('WARNING', 'fundamentals', f'{ticker}: AV fetch failed: {e}')
    return {}


def _av_to_yf_shape(av: dict) -> dict:
    """Map Alpha Vantage OVERVIEW keys to our normalised yf_info shape."""
    mc = _safe_float(av.get('MarketCapitalization'))
    return {
        'market_cap':     mc,
        'pe_trailing':    _safe_float(av.get('PERatio')),
        'pe_forward':     _safe_float(av.get('ForwardPE')),
        'dividend_yield': _safe_float(av.get('DividendYield')),
        'beta':           _safe_float(av.get('Beta')),
        'week52_high':    _safe_float(av.get('52WeekHigh')),
        'week52_low':     _safe_float(av.get('52WeekLow')),
        'revenue_growth': _safe_float(av.get('QuarterlyRevenueGrowthYOY')),
        'gross_margin':   _safe_float(av.get('GrossProfitTTM')),
        'roe':            _safe_float(av.get('ReturnOnEquityTTM')),
        'analyst_target': _safe_float(av.get('AnalystTargetPrice')),
        'employees':      _safe_float(av.get('FullTimeEmployees')),
        'description':    (av.get('Description') or '')[:400],
        'sector':         av.get('Sector', ''),
        'industry':       av.get('Industry', ''),
        '_source':        'alphavantage',
    }


# ── Main builder ──────────────────────────────────────────────────────────────

def build_yf_info(ticker: str) -> dict:
    """
    Build the full fundamentals dict for the ticker detail page.

    Strategy:
      1. Try yfinance .info (cached 12 h) — richest, works for most tickers
      2. If missing key ratios, try Alpha Vantage (cached 24 h) to fill gaps
      3. Always overlay fast_info for live price fields (they bypass rate limits)
    """
    info = {}

    # Step 1 — try yfinance .info (primary)
    yf = get_yf_info(ticker)
    if yf:
        info = dict(yf)

    # Step 2 — if yfinance gave nothing useful, try Alpha Vantage
    if not info.get('market_cap') and not info.get('pe_trailing'):
        av = get_av_overview(ticker)
        if av.get('Symbol'):
            info = _av_to_yf_shape(av)

    # Step 3 — overlay fast_info for live price fields (no crumb, always works)
    fi = get_fast_info(ticker)
    for key in ('market_cap', 'last_price', 'week52_high', 'week52_low',
                'currency', 'prev_close', 'avg_vol_3m'):
        live = fi.get(key)
        if live is not None:
            info[key] = live   # prefer live value

    return info


# ── Pre-fetch for pipeline ────────────────────────────────────────────────────

def prefetch_fundamentals(extra_tickers: list = None):
    """
    Pre-populate yfinance + AV caches for the most-viewed tickers.
    Priority: holdings → watchlist → today's recommendations → extra_tickers.

    yfinance .info is tried first; AV used as fallback for tickers that fail.
    AV calls capped at MAX_ALPHA_VANTAGE_CALLS (default 22) per day.
    """
    from dotenv import load_dotenv
    load_dotenv('/home/ubuntu/advisor/.env')

    max_av_calls = int(os.environ.get('MAX_ALPHA_VANTAGE_CALLS', 22))

    seen, ordered = set(), []

    def _add(tickers):
        for t in tickers:
            if t not in seen:
                seen.add(t); ordered.append(t)

    _add([r['ticker'] for r in query("SELECT ticker FROM holdings WHERE active=TRUE AND shares>0")])
    _add([r['ticker'] for r in query("SELECT ticker FROM watchlist WHERE active=TRUE")])
    _add([r['ticker'] for r in query("SELECT DISTINCT ticker FROM recommendations WHERE date=%s",
                                      (date.today(),))])
    if extra_tickers:
        _add(extra_tickers)

    if not ordered:
        log('INFO', 'fundamentals', 'No tickers to prefetch')
        return

    # Split: tickers needing yfinance refresh vs AV fallback candidates
    yf_stale, av_stale = [], []
    for t in ordered:
        yf_cf = _yf_cache_path(t)
        av_cf = AV_CACHE_DIR / f'{t}.json'
        yf_fresh = yf_cf.exists() and (time.time() - yf_cf.stat().st_mtime) < YF_CACHE_TTL
        av_fresh = av_cf.exists() and (time.time() - av_cf.stat().st_mtime) < AV_CACHE_TTL
        if not yf_fresh:
            yf_stale.append(t)
        if not av_fresh:
            av_stale.append(t)

    log('INFO', 'fundamentals',
        f'Prefetch: {len(yf_stale)} yfinance stale, {len(av_stale)} AV stale '
        f'out of {len(ordered)} tickers')

    yf_ok, yf_fail = 0, []
    for ticker in yf_stale:
        result = get_yf_info(ticker)
        if result.get('market_cap') or result.get('pe_trailing'):
            yf_ok += 1
        else:
            yf_fail.append(ticker)
        time.sleep(0.3)

    # AV fallback for tickers that failed yfinance (up to daily limit)
    av_targets = [t for t in yf_fail if t in av_stale][:max_av_calls]
    av_ok = 0
    for ticker in av_targets:
        av = get_av_overview(ticker)
        if av.get('Symbol'):
            av_ok += 1
        time.sleep(0.5)

    log('INFO', 'fundamentals',
        f'Prefetch complete: {yf_ok} via yfinance, {av_ok} via AV fallback, '
        f'{len(yf_fail) - av_ok} no data (likely European/unlisted)')


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv('/home/ubuntu/advisor/.env')
    prefetch_fundamentals()
