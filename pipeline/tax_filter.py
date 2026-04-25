"""
Finnish tax filter (MiFID II compliance).
Runs on discovery candidates before TradingAgents.
Blocks US ETFs, adds tax metadata.
"""
import sys
from datetime import date

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log

# UCITS equivalents for common US ETFs (MiFID II safe alternatives)
UCITS_EQUIVALENTS = {
    'SPY': 'CSPX.L (iShares Core S&P 500 UCITS)',
    'QQQ': 'CNDX.L (iShares NASDAQ 100 UCITS)',
    'VTI': 'VWRL.L (Vanguard FTSE All-World UCITS)',
    'IWM': 'ZPRR.DE (SPDR Russell 2000 UCITS)',
    'GLD': 'IGLN.L (iShares Physical Gold ETC)',
    'EFA': 'IWRD.L (iShares MSCI World UCITS)',
    'EEM': 'IDEM.L (iShares MSCI Emerging Markets UCITS)',
    'AGG': 'AGGG.L (iShares Global Aggregate Bond UCITS)',
    'TLT': 'IDTL.L (iShares $ Treasury Bond 20+yr UCITS)',
    'LQD': 'SLXX.L (iShares Core $ Corp Bond UCITS)',
    'HYG': 'IHYU.L (iShares $ High Yield Corp Bond UCITS)',
    'VNQ': 'IUSP.L (iShares US Property Yield UCITS)',
    'XLF': 'XDWF.L (Xtrackers MSCI World Financials UCITS)',
    'XLK': 'XDWT.L (Xtrackers MSCI World IT UCITS)',
    'XLE': 'XDWN.L (Xtrackers MSCI World Energy UCITS)',
    'XLV': 'XDWH.L (Xtrackers MSCI World Health Care UCITS)',
    'ARKK': None,   # No direct UCITS equivalent
    'VOO': 'VUAA.L (Vanguard S&P 500 UCITS)',
    'VGT': 'IUIT.L (iShares S&P 500 IT Sector UCITS)',
}

# Common US ETF ticker patterns
ETF_SUFFIXES = ('ETF', 'FUND', 'TRUST')
ETF_PREFIXES = ('XL', 'VN', 'GD', 'TL', 'AG', 'HY', 'IW', 'EF')

# Withholding tax rates
WITHHOLDING_TAX = {
    'US': 15,   # US-Finland treaty rate
    'GB': 0,
    'DE': 0,
    'FR': 0,
    'FI': 0,
    'SE': 0,
    'NL': 15,   # Netherlands has 15% WHT for dividends
    'CH': 35,   # Switzerland has high WHT — reclaim possible
    'JP': 10,
    'HK': 0,
    'AU': 15,
    'ES': 15,
    'DK': 27,   # Denmark WHT (reclaim to 15% possible)
}

# Nordnet tradeable exchanges
NORDNET_EXCHANGES = {
    'NYSE', 'NASDAQ', 'NYSE/NASDAQ', 'LSE', 'XETRA',
    'Euronext Paris', 'Euronext Amsterdam', 'Nasdaq Helsinki',
    'Nasdaq Stockholm', 'BME', 'SIX', 'TSE', 'HKEX', 'ASX',
}


def _is_us_etf(ticker, asset_type=None):
    """Detect if a ticker is a US ETF (blocked under MiFID II)."""
    if asset_type and asset_type.upper() == 'ETF':
        return True
    # Pattern-based detection for known US ETFs
    ticker_clean = ticker.split('.')[0].upper()
    if ticker_clean in UCITS_EQUIVALENTS:
        return True
    # 3-letter all-caps with no exchange suffix typical of US ETFs
    if len(ticker_clean) <= 4 and '.' not in ticker:
        if any(ticker_clean.startswith(p) for p in ETF_PREFIXES):
            return True
    return False


def _get_country_for_ticker(ticker):
    """Look up country from universe table."""
    rows = query(
        "SELECT country, exchange, asset_type FROM universe WHERE ticker=%s",
        (ticker,)
    )
    if rows:
        return rows[0]['country'], rows[0]['exchange'], rows[0].get('asset_type', 'stock')
    # Infer from suffix
    if ticker.endswith('.L'):
        return 'GB', 'LSE', 'stock'
    if ticker.endswith('.DE'):
        return 'DE', 'XETRA', 'stock'
    if ticker.endswith('.HE'):
        return 'FI', 'Nasdaq Helsinki', 'stock'
    if ticker.endswith('.ST'):
        return 'SE', 'Nasdaq Stockholm', 'stock'
    if ticker.endswith('.PA'):
        return 'FR', 'Euronext Paris', 'stock'
    if ticker.endswith('.AS'):
        return 'NL', 'Euronext Amsterdam', 'stock'
    if ticker.endswith('.SW'):
        return 'CH', 'SIX', 'stock'
    if ticker.endswith('.T'):
        return 'JP', 'TSE', 'stock'
    if ticker.endswith('.HK'):
        return 'HK', 'HKEX', 'stock'
    if ticker.endswith('.AX'):
        return 'AU', 'ASX', 'stock'
    return 'US', 'NYSE/NASDAQ', 'stock'


def _get_currency_for_country(country):
    mapping = {
        'US': 'USD', 'GB': 'GBP', 'DE': 'EUR', 'FR': 'EUR',
        'FI': 'EUR', 'SE': 'SEK', 'NL': 'EUR', 'CH': 'CHF',
        'JP': 'JPY', 'HK': 'HKD', 'AU': 'AUD', 'ES': 'EUR',
        'DK': 'DKK',
    }
    return mapping.get(country, 'EUR')


def _fx_risk(currency):
    if currency == 'EUR':
        return 'low'
    if currency in ('USD', 'GBP', 'CHF'):
        return 'medium'
    return 'high'


def apply_tax_filter(candidates):
    """
    Apply Finnish tax filter to candidate list.
    Returns list of candidates with tax metadata added.
    eligible=True means the stock can be traded by a Finnish investor.
    """
    log('INFO', 'tax_filter', f'Applying tax filter to {len(candidates)} candidates...')
    results = []
    blocked = 0

    for c in candidates:
        ticker = c['ticker']
        country, exchange, asset_type = _get_country_for_ticker(ticker)

        # Block US ETFs (MiFID II)
        if country == 'US' and _is_us_etf(ticker, asset_type):
            ucits = UCITS_EQUIVALENTS.get(ticker.split('.')[0].upper())
            note = f'BLOCKED: US ETF not available to EU retail investors (MiFID II/PRIIPs)'
            if ucits:
                note += f'. UCITS alternative: {ucits}'
            log('WARNING', 'tax_filter', f'{ticker}: {note}')
            results.append({
                **c,
                'eligible': False,
                'block_reason': note,
                'tax_notes': note,
                'dividend_withholding_pct': 0,
                'currency': 'USD',
                'fx_risk': 'medium',
                'nordnet_tradeable': False,
            })
            blocked += 1
            continue

        # Tax metadata
        wht = WITHHOLDING_TAX.get(country, 15)
        currency = _get_currency_for_country(country)
        fx_risk = _fx_risk(currency)
        nordnet_ok = exchange in NORDNET_EXCHANGES

        tax_notes = []
        if wht > 0:
            tax_notes.append(f'{wht}% dividend withholding tax ({country})')
        if country == 'CH':
            tax_notes.append('Swiss WHT 35% — reclaim 20% via Finland-CH treaty')
        if country == 'DK':
            tax_notes.append('Danish WHT 27% — reclaim to 15% via treaty')
        if currency != 'EUR':
            tax_notes.append(f'FX exposure: {currency}/EUR')
        if not nordnet_ok:
            tax_notes.append(f'Exchange {exchange} may not be on Nordnet')

        tax_notes_str = '; '.join(tax_notes) if tax_notes else 'No special tax considerations'
        result = {
            **c,
            'eligible': True,
            'block_reason': None,
            'dividend_withholding_pct': wht,
            'currency': currency,
            'fx_risk': fx_risk,
            'nordnet_tradeable': nordnet_ok,
            'tax_notes': tax_notes_str,
            'country': country,
            'exchange': exchange,
        }
        # Persist tax metadata to discovery_candidates
        candidate_id = c.get('id')
        if candidate_id:
            execute("""
                UPDATE discovery_candidates
                SET currency=%s, fx_risk=%s, tax_notes=%s,
                    dividend_withholding_pct=%s, eligible=TRUE
                WHERE id=%s
            """, (currency, fx_risk, tax_notes_str[:500], wht, candidate_id))
        results.append(result)

    eligible = [r for r in results if r['eligible']]
    log('INFO', 'tax_filter',
        f'Tax filter complete: {len(eligible)} eligible, {blocked} blocked')
    return results


if __name__ == '__main__':
    # Test with sample candidates
    from pipeline.discovery.event_classifier import classify_events
    from pipeline.discovery.event_mapper import map_events
    from pipeline.discovery.scorer import score_candidates

    events = classify_events()
    map_events(events)
    top = score_candidates()

    filtered = apply_tax_filter(top)
    print(f'\n--- Tax filter results ---')
    eligible = [r for r in filtered if r['eligible']]
    blocked = [r for r in filtered if not r['eligible']]
    print(f'Eligible: {len(eligible)}, Blocked: {len(blocked)}')

    for r in eligible[:10]:
        print(f"  {r['direction']:4s} {r['ticker']:8s} "
              f"score={r.get('total_score', 0):5.1f} "
              f"WHT={r['dividend_withholding_pct']}% "
              f"currency={r['currency']}")
    for r in blocked:
        print(f"  BLOCKED {r['ticker']}: {r['block_reason'][:60]}")
