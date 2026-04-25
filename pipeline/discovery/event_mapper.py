"""
Event mapper — maps detected events to affected stocks in universe table.
Uses sector tags, never hardcodes specific tickers.
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log

EVENT_NARRATIVE = {
    'HEALTH_CRISIS':      ('Health/pandemic event detected',
                           'Pharma, biotech & healthcare benefit from increased demand',
                           'Airlines, hotels & leisure face reduced demand'),
    'GEOPOLITICAL':       ('Geopolitical tension / conflict detected',
                           'Defense & energy sectors benefit from heightened risk',
                           'Airlines, tourism & autos face demand headwinds'),
    'TECH_BREAKTHROUGH':  ('Technology breakthrough / AI momentum detected',
                           'Semiconductors, software & cloud computing are direct beneficiaries',
                           'Traditional media & retail face competitive disruption'),
    'MACRO_SHIFT':        ('Macro / monetary policy shift detected',
                           'Banks & financials benefit from rate environment',
                           'Real estate, utilities & REITs face higher discount rates'),
    'ENERGY':             ('Energy market event detected',
                           'Oil & gas, mining & energy producers benefit from price moves',
                           'Airlines, transport & chemicals face higher input costs'),
    'SUPPLY_CHAIN':       ('Supply chain disruption detected',
                           'Logistics, shipping & warehousing benefit from disruption',
                           'Manufacturing, retail & consumer electronics face cost pressures'),
    'REGULATORY':         ('Regulatory / legal action detected',
                           'Compliance & legal services benefit',
                           'Tech & pharma face regulatory headwinds'),
}

EVENT_SECTOR_MAP = {
    'HEALTH_CRISIS': {
        'buy_sectors': ['Healthcare', 'Biotechnology', 'Pharmaceuticals', 'Drug Manufacturers'],
        'sell_sectors': ['Airlines', 'Hotels', 'Leisure', 'Cruise Lines', 'Hospitality'],
        'buy_countries': [],
        'sell_countries': [],
    },
    'GEOPOLITICAL': {
        'buy_sectors': ['Aerospace & Defense', 'Energy', 'Oil & Gas', 'Defense'],
        'sell_sectors': ['Airlines', 'Tourism', 'Automobiles', 'Auto Manufacturers'],
        'buy_countries': ['US', 'GB'],
        'sell_countries': ['RU', 'CN'],
    },
    'TECH_BREAKTHROUGH': {
        'buy_sectors': ['Semiconductors', 'Technology', 'Software', 'Cloud Computing',
                        'Information Technology'],
        'sell_sectors': ['Traditional Media', 'Retail', 'Communication Services'],
        'buy_countries': [],
        'sell_countries': [],
    },
    'MACRO_SHIFT': {
        'buy_sectors': ['Banking', 'Insurance', 'Financial Services', 'Financials'],
        'sell_sectors': ['Real Estate', 'Utilities', 'REITs', 'Consumer Staples'],
        'buy_countries': [],
        'sell_countries': [],
    },
    'ENERGY': {
        'buy_sectors': ['Oil & Gas', 'Energy', 'Mining', 'Natural Resources', 'Basic Materials'],
        'sell_sectors': ['Airlines', 'Transportation', 'Chemicals', 'Industrials'],
        'buy_countries': [],
        'sell_countries': [],
    },
    'SUPPLY_CHAIN': {
        'buy_sectors': ['Logistics', 'Shipping', 'Warehousing', 'Industrials', 'Transportation'],
        'sell_sectors': ['Manufacturing', 'Retail', 'Consumer Electronics', 'Consumer Discretionary'],
        'buy_countries': [],
        'sell_countries': [],
    },
    'REGULATORY': {
        'buy_sectors': ['Legal Services', 'Compliance', 'Financials'],
        'sell_sectors': ['Technology', 'Pharmaceuticals', 'Financials'],
        'buy_countries': [],
        'sell_countries': [],
    },
}


def _sector_matches(stock_sector, target_sectors):
    """Fuzzy match — any target sector appearing in the stock's sector string."""
    if not stock_sector:
        return False
    stock_lower = stock_sector.lower()
    return any(t.lower() in stock_lower or stock_lower in t.lower()
               for t in target_sectors)


def _get_universe_by_sector(sectors, countries=None):
    """Query universe for stocks matching any of the given sectors."""
    if not sectors:
        return []
    rows = query("SELECT ticker, sector, country FROM universe WHERE validated=TRUE AND active=TRUE")
    results = []
    for row in rows:
        if _sector_matches(row['sector'], sectors):
            if countries and row['country'] not in countries:
                continue
            results.append(row)
    return results


def _get_momentum_score(ticker):
    """Get price momentum from prices table."""
    rows = query("""
        SELECT close FROM prices
        WHERE ticker = %s
        ORDER BY date DESC LIMIT 30
    """, (ticker,))
    if len(rows) < 5:
        return 0.0
    latest = float(rows[0]['close'])
    oldest = float(rows[-1]['close'])
    return round(((latest - oldest) / oldest) * 100, 2) if oldest else 0.0


def _get_velocity(ticker):
    """Get mention_velocity from news_sentiment."""
    rows = query("""
        SELECT mention_velocity FROM news_sentiment
        WHERE ticker = %s
        ORDER BY date DESC LIMIT 1
    """, (ticker,))
    if rows and rows[0]['mention_velocity'] is not None:
        return float(rows[0]['mention_velocity'])
    return 1.0


def _get_polymarket_confirmation(event_type):
    """Check if Polymarket has high-probability signal for this event type."""
    config = {}
    keywords = {
        'HEALTH_CRISIS': ['pandemic', 'vaccine', 'covid'],
        'GEOPOLITICAL': ['war', 'ukraine', 'russia', 'china', 'taiwan'],
        'TECH_BREAKTHROUGH': ['ai', 'nvidia', 'semiconductor'],
        'MACRO_SHIFT': ['rate', 'fed', 'recession', 'inflation'],
        'ENERGY': ['oil', 'opec', 'energy'],
        'SUPPLY_CHAIN': ['tariff', 'supply', 'trade'],
        'REGULATORY': ['antitrust', 'sec', 'regulation'],
    }.get(event_type, [])

    markets = query("""
        SELECT question, probability FROM polymarket_signals
        WHERE date >= %s
        ORDER BY date DESC LIMIT 100
    """, (date.today() - timedelta(days=1),))

    for market in markets:
        q = (market['question'] or '').lower()
        if any(kw in q for kw in keywords):
            prob = float(market['probability'] or 0.5)
            if prob > 0.6 or prob < 0.3:
                return True
    return False


def _save_candidate(ticker, direction, event_type, reason, base_score):
    execute("""
        INSERT INTO discovery_candidates
            (date, ticker, direction, event_type, reason, total_score, analyzed)
        VALUES (%s, %s, %s, %s, %s, %s, FALSE)
        ON CONFLICT DO NOTHING
    """, (date.today(), ticker, direction, event_type, reason[:500],
          round(base_score, 4)))


def map_events(detected_events):
    """Map each detected event to buy/sell candidates. Returns list of candidates."""
    log('INFO', 'event_mapper', f'Mapping {len(detected_events)} events to stocks...')
    all_candidates = []
    saved = 0

    for event in detected_events:
        event_type = event['event_type']
        confidence = event['confidence']
        sector_map = EVENT_SECTOR_MAP.get(event_type, {})

        poly_confirm = _get_polymarket_confirmation(event_type)

        # BUY candidates
        buy_sectors = sector_map.get('buy_sectors', [])
        buy_countries = sector_map.get('buy_countries', []) or None
        buy_stocks = _get_universe_by_sector(buy_sectors, buy_countries)

        narrative = EVENT_NARRATIVE.get(event_type, (event_type, '', ''))
        event_title, buy_rationale, sell_rationale = narrative

        for stock in buy_stocks[:30]:  # cap per event
            ticker = stock['ticker']
            velocity = _get_velocity(ticker)
            momentum = _get_momentum_score(ticker)

            momentum_aligned = momentum > 2.0
            already_accelerating = velocity > 1.5

            base_score = confidence
            if already_accelerating:
                base_score = min(1.0, base_score + 0.1)
            if momentum_aligned:
                base_score = min(1.0, base_score + 0.05)
            if poly_confirm:
                base_score = min(1.0, base_score + 0.1)

            signals = []
            if already_accelerating:
                signals.append(f'GDELT {velocity:.1f}x velocity ↑')
            if momentum_aligned:
                signals.append(f'price momentum {momentum:+.1f}%')
            if poly_confirm:
                signals.append('Polymarket confirmed')
            signals_str = '; '.join(signals) or f'momentum={momentum:+.1f}%'

            reason = (f"{event_title}. {buy_rationale}. "
                      f"Sector: {stock['sector']}. Signals: {signals_str}.")

            _save_candidate(ticker, 'BUY', event_type, reason, base_score)
            all_candidates.append({
                'ticker': ticker, 'direction': 'BUY',
                'event_type': event_type, 'score': base_score, 'reason': reason,
            })
            saved += 1

        # SELL candidates — these are sector headwinds, not necessarily held positions
        # They are labeled SELL to indicate: if held, consider exiting; if watching, avoid entry
        sell_sectors = sector_map.get('sell_sectors', [])
        sell_countries = sector_map.get('sell_countries', []) or None
        sell_stocks = _get_universe_by_sector(sell_sectors, sell_countries)

        for stock in sell_stocks[:20]:
            ticker = stock['ticker']
            momentum = _get_momentum_score(ticker)
            momentum_down = momentum < -2.0

            base_score = confidence
            if momentum_down:
                base_score = min(1.0, base_score + 0.1)
            if poly_confirm:
                base_score = min(1.0, base_score + 0.1)

            signals = []
            if momentum_down:
                signals.append(f'price declining {momentum:+.1f}%')
            if poly_confirm:
                signals.append('Polymarket confirmed')
            signals_str = '; '.join(signals) or f'momentum={momentum:+.1f}%'

            reason = (f"{event_title}. {sell_rationale}. "
                      f"Sector: {stock['sector']}. "
                      f"If held: consider exit. If not held: avoid entry. Signals: {signals_str}.")

            _save_candidate(ticker, 'SELL', event_type, reason, base_score)
            all_candidates.append({
                'ticker': ticker, 'direction': 'SELL',
                'event_type': event_type, 'score': base_score, 'reason': reason,
            })
            saved += 1

    log('INFO', 'event_mapper', f'Saved {saved} discovery candidates')
    return all_candidates


if __name__ == '__main__':
    from pipeline.discovery.event_classifier import classify_events
    events = classify_events()
    candidates = map_events(events)
    print(f'\n--- Discovery candidates: {len(candidates)} ---')
    buy = [c for c in candidates if c['direction'] == 'BUY']
    sell = [c for c in candidates if c['direction'] == 'SELL']
    print(f'BUY: {len(buy)}, SELL: {len(sell)}')
    for c in sorted(candidates, key=lambda x: -x['score'])[:10]:
        print(f"  {c['direction']} {c['ticker']} ({c['event_type']}) score={c['score']:.2f}")
