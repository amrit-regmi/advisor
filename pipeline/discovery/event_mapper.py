"""
Event mapper — maps detected events to affected stocks in universe table.
Uses sector tags, never hardcodes specific tickers.

Polarity logic
--------------
Most events are unidirectional (HEALTH_CRISIS, GEOPOLITICAL, TECH_BREAKTHROUGH,
SUPPLY_CHAIN, REGULATORY): buy/sell sectors are fixed.

Bidirectional events (ENERGY, MACRO_SHIFT) can swing either way depending on
what the data says:
  ENERGY     → bullish if energy-sector momentum is positive (oil rising)
             → bearish if energy-sector momentum is negative (oil falling)
  MACRO_SHIFT→ hawkish (rates rising)  → buy banks, sell REITs
             → dovish (rates falling)  → buy REITs, sell banks

Per-stock momentum override
---------------------------
Even within the correct polarity, individual stocks are re-evaluated:
  • A BUY candidate whose own momentum is strongly negative (< -3%) is flipped
    to SELL — price is screaming the opposite of the event thesis.
  • A SELL candidate whose momentum is strongly positive (> +3%) is flipped to
    BUY — the "bad news" is already priced in and price is recovering.
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log

# ---------------------------------------------------------------------------
# Static narratives — tuples of (title, buy_rationale, sell_rationale)
# ---------------------------------------------------------------------------
EVENT_NARRATIVE = {
    # Health — worsening outbreak vs resolving
    'HEALTH_CRISIS':          ('Health/pandemic event worsening',
                               'Pharma, biotech & healthcare benefit from increased demand',
                               'Airlines, hotels & leisure face reduced demand'),
    'HEALTH_CRISIS_RESOLVING':('Health/pandemic event resolving',
                               'Airlines, hotels & leisure recover as restrictions ease',
                               'Pharma & biotech lose crisis-driven demand tailwind'),
    # Geopolitical — escalation vs de-escalation
    'GEOPOLITICAL':            ('Geopolitical tension / escalation detected',
                                'Defense & energy sectors benefit from heightened risk',
                                'Airlines, tourism & autos face demand headwinds'),
    'GEOPOLITICAL_RESOLVING':  ('Geopolitical tension easing / de-escalation detected',
                                'Airlines, tourism & consumer sectors recover',
                                'Defense & crisis-driven energy demand fades'),
    # Tech — always bullish for tech (breakthroughs don't reverse into SELL tech)
    'TECH_BREAKTHROUGH':       ('Technology breakthrough / AI momentum detected',
                                'Semiconductors, software & cloud computing are direct beneficiaries',
                                'Traditional media & retail face competitive disruption'),
    # Macro — hawkish vs dovish
    'MACRO_SHIFT':             ('Macro shift: hawkish / rate-rising environment detected',
                                'Banks & financials benefit from higher net-interest margins',
                                'Real estate, utilities & REITs face higher discount rates'),
    'MACRO_SHIFT_DOVISH':      ('Macro shift: dovish / rate-falling environment detected',
                                'Real estate, utilities & REITs benefit from lower discount rates',
                                'Banks & financials face net-interest-margin compression'),
    # Energy — rising prices vs falling prices
    'ENERGY':                  ('Energy prices rising — bullish for producers',
                                'Oil & gas, mining & energy producers benefit from price moves',
                                'Airlines, transport & chemicals face higher input costs'),
    'ENERGY_BEARISH':          ('Energy prices falling — bearish for producers',
                                'Airlines, transport & consumers benefit from lower fuel costs',
                                'Oil & gas, energy producers face lower revenues'),
    # Supply chain — disruption vs resolution
    'SUPPLY_CHAIN':            ('Supply-chain disruption detected',
                                'Logistics, shipping & warehousing benefit from disruption premium',
                                'Manufacturing, retail & consumer electronics face cost pressures'),
    'SUPPLY_CHAIN_RESOLVING':  ('Supply-chain disruption easing / resolving',
                                'Manufacturing, retail & consumer electronics recover on lower costs',
                                'Logistics & shipping lose disruption-driven pricing power'),
    # Regulatory — crackdown vs relaxation
    'REGULATORY':              ('Regulatory crackdown / new restrictions detected',
                                'Compliance & legal services benefit',
                                'Tech & pharma face regulatory headwinds'),
    'REGULATORY_RELAXING':     ('Regulatory relaxation / deregulation detected',
                                'Tech & pharma benefit from lighter regulatory burden',
                                'Compliance & legal services lose enforcement-driven demand'),
}

# ---------------------------------------------------------------------------
# Sector maps — for bidirectional events both the bullish and bearish maps
# are stored; polarity detection selects which to apply at runtime.
# ---------------------------------------------------------------------------
EVENT_SECTOR_MAP = {
    # ── Health ───────────────────────────────────────────────────────────────
    'HEALTH_CRISIS': {
        'buy_sectors':    ['Healthcare', 'Biotechnology', 'Pharmaceuticals', 'Drug Manufacturers'],
        'sell_sectors':   ['Airlines', 'Hotels', 'Leisure', 'Cruise Lines', 'Hospitality'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    'HEALTH_CRISIS_RESOLVING': {
        'buy_sectors':    ['Airlines', 'Hotels', 'Leisure', 'Cruise Lines', 'Hospitality'],
        'sell_sectors':   ['Healthcare', 'Biotechnology', 'Pharmaceuticals', 'Drug Manufacturers'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    # ── Geopolitical ─────────────────────────────────────────────────────────
    'GEOPOLITICAL': {
        'buy_sectors':    ['Aerospace & Defense', 'Energy', 'Oil & Gas', 'Defense'],
        'sell_sectors':   ['Airlines', 'Tourism', 'Automobiles', 'Auto Manufacturers'],
        'buy_countries':  ['US', 'GB'],
        'sell_countries': ['RU', 'CN'],
    },
    'GEOPOLITICAL_RESOLVING': {
        'buy_sectors':    ['Airlines', 'Tourism', 'Automobiles', 'Consumer Discretionary'],
        'sell_sectors':   ['Aerospace & Defense', 'Defense'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    # ── Tech (unidirectional — breakthroughs don't become sell signals) ──────
    'TECH_BREAKTHROUGH': {
        'buy_sectors':    ['Semiconductors', 'Technology', 'Software', 'Cloud Computing',
                           'Information Technology'],
        'sell_sectors':   ['Traditional Media', 'Retail', 'Communication Services'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    # ── Macro ────────────────────────────────────────────────────────────────
    'MACRO_SHIFT': {
        'buy_sectors':    ['Banking', 'Insurance', 'Financial Services', 'Financials'],
        'sell_sectors':   ['Real Estate', 'Utilities', 'REITs', 'Consumer Staples'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    'MACRO_SHIFT_DOVISH': {
        'buy_sectors':    ['Real Estate', 'Utilities', 'REITs', 'Consumer Staples'],
        'sell_sectors':   ['Banking', 'Insurance', 'Financial Services', 'Financials'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    # ── Energy ───────────────────────────────────────────────────────────────
    'ENERGY': {
        'buy_sectors':    ['Oil & Gas', 'Energy', 'Mining', 'Natural Resources', 'Basic Materials'],
        'sell_sectors':   ['Airlines', 'Transportation', 'Chemicals', 'Industrials'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    'ENERGY_BEARISH': {
        'buy_sectors':    ['Airlines', 'Transportation', 'Chemicals', 'Consumer Discretionary'],
        'sell_sectors':   ['Oil & Gas', 'Energy', 'Mining', 'Natural Resources', 'Basic Materials'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    # ── Supply chain ─────────────────────────────────────────────────────────
    'SUPPLY_CHAIN': {
        'buy_sectors':    ['Logistics', 'Shipping', 'Warehousing', 'Industrials', 'Transportation'],
        'sell_sectors':   ['Manufacturing', 'Retail', 'Consumer Electronics', 'Consumer Discretionary'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    'SUPPLY_CHAIN_RESOLVING': {
        'buy_sectors':    ['Manufacturing', 'Retail', 'Consumer Electronics', 'Consumer Discretionary'],
        'sell_sectors':   ['Logistics', 'Shipping', 'Warehousing'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    # ── Regulatory ───────────────────────────────────────────────────────────
    'REGULATORY': {
        'buy_sectors':    ['Legal Services', 'Compliance', 'Financials'],
        'sell_sectors':   ['Technology', 'Pharmaceuticals', 'Financials'],
        'buy_countries':  [],
        'sell_countries': [],
    },
    'REGULATORY_RELAXING': {
        'buy_sectors':    ['Technology', 'Pharmaceuticals', 'Financials'],
        'sell_sectors':   ['Legal Services', 'Compliance'],
        'buy_countries':  [],
        'sell_countries': [],
    },
}

# All events where polarity must be detected at runtime from market data.
# TECH_BREAKTHROUGH is intentionally excluded — a tech breakthrough is always
# bullish for tech; the event_classifier wouldn't fire it on a tech crash.
BIDIRECTIONAL_EVENTS = {
    'ENERGY', 'MACRO_SHIFT', 'HEALTH_CRISIS', 'GEOPOLITICAL',
    'SUPPLY_CHAIN', 'REGULATORY',
}

# Momentum threshold for per-stock direction flip
MOMENTUM_FLIP_THRESHOLD = 3.0   # flip BUY→SELL if stock momentum < -3%
MOMENTUM_CONFIRM_THRESHOLD = 2.0 # bonus when momentum > +2%


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sector_matches(stock_sector, target_sectors):
    """Fuzzy match — any target sector appearing in the stock's sector string."""
    if not stock_sector:
        return False
    stock_lower = stock_sector.lower()
    return any(t.lower() in stock_lower or stock_lower in t.lower()
               for t in target_sectors)


def _get_universe_by_sector(sectors, countries=None):
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
    """30-day price return (%)."""
    rows = query("""
        SELECT close FROM prices
        WHERE ticker = %s ORDER BY date DESC LIMIT 30
    """, (ticker,))
    if len(rows) < 5:
        return 0.0
    latest = float(rows[0]['close'])
    oldest = float(rows[-1]['close'])
    return round(((latest - oldest) / oldest) * 100, 2) if oldest else 0.0


def _get_sector_avg_momentum(sectors):
    """Average 30-day momentum across a sample of universe stocks in given sectors."""
    stocks = _get_universe_by_sector(sectors)
    if not stocks:
        return 0.0
    scores = []
    for s in stocks[:20]:  # sample — no need to scan all
        m = _get_momentum_score(s['ticker'])
        if m != 0.0:
            scores.append(m)
    return sum(scores) / len(scores) if scores else 0.0


def _get_velocity(ticker):
    rows = query("""
        SELECT mention_velocity FROM news_sentiment
        WHERE ticker = %s ORDER BY date DESC LIMIT 1
    """, (ticker,))
    if rows and rows[0]['mention_velocity'] is not None:
        return float(rows[0]['mention_velocity'])
    return 1.0


def _get_gdelt_tone(ticker):
    """Average GDELT tone over last 3 days (positive = bullish)."""
    rows = query("""
        SELECT avg_tone FROM news_sentiment
        WHERE ticker = %s AND date >= %s ORDER BY date DESC LIMIT 3
    """, (ticker, date.today() - timedelta(days=3)))
    if not rows:
        return 0.0
    return sum(float(r['avg_tone'] or 0) for r in rows) / len(rows)


def _get_macro_rate_direction():
    """
    Positive return → rates are rising (hawkish).
    Negative return → rates are falling (dovish).
    Uses FEDFUNDS from macro_data; falls back to 10Y/2Y yield curve shape.
    """
    rows = query("""
        SELECT value, date FROM macro_data
        WHERE series_id = 'FEDFUNDS'
        ORDER BY date DESC LIMIT 6
    """)
    if len(rows) >= 2:
        latest = float(rows[0]['value'] or 0)
        earlier = float(rows[-1]['value'] or 0)
        return latest - earlier  # positive = rates rising
    return 0.0


def _get_polymarket_confirmation(event_type):
    """Check Polymarket for high-probability signal matching the event."""
    keywords = {
        'HEALTH_CRISIS':     ['pandemic', 'vaccine', 'covid'],
        'GEOPOLITICAL':      ['war', 'ukraine', 'russia', 'china', 'taiwan'],
        'TECH_BREAKTHROUGH': ['ai', 'nvidia', 'semiconductor'],
        'MACRO_SHIFT':       ['rate', 'fed', 'recession', 'inflation'],
        'ENERGY':            ['oil', 'opec', 'energy'],
        'SUPPLY_CHAIN':      ['tariff', 'supply', 'trade'],
        'REGULATORY':        ['antitrust', 'sec', 'regulation'],
    }.get(event_type.replace('_BEARISH', '').replace('_DOVISH', ''), [])

    markets = query("""
        SELECT question, probability FROM polymarket_signals
        WHERE date >= %s ORDER BY date DESC LIMIT 100
    """, (date.today() - timedelta(days=1),))

    for market in markets:
        q = (market['question'] or '').lower()
        if any(kw in q for kw in keywords):
            prob = float(market['probability'] or 0.5)
            if prob > 0.6 or prob < 0.3:
                return True
    return False


def _detect_event_polarity(event_type):
    """
    For every bidirectional event, decide which variant to use based on live
    market data.  Returns the resolved event_type key (e.g. 'ENERGY_BEARISH').

    Decision logic per event
    ────────────────────────
    ENERGY          : primary-sector (oil/energy) avg momentum
                      positive → bullish (producers win)
                      negative → ENERGY_BEARISH (consumers win)

    MACRO_SHIFT     : FEDFUNDS rate delta over last ~6 observations
                      rising  → hawkish (banks win)
                      falling → MACRO_SHIFT_DOVISH (REITs win)

    HEALTH_CRISIS   : primary-sector (healthcare/pharma) avg momentum
                      positive → worsening crisis (pharma demand rising)
                      negative → HEALTH_CRISIS_RESOLVING (crisis easing)

    GEOPOLITICAL    : primary-sector (defense) avg momentum
                      positive → escalation (defense spending rising)
                      negative → GEOPOLITICAL_RESOLVING (tensions easing)

    SUPPLY_CHAIN    : primary-sector (logistics/shipping) avg momentum
                      positive → disruption ongoing (logistics premium)
                      negative → SUPPLY_CHAIN_RESOLVING (normalising)

    REGULATORY      : GDELT avg tone for regulated sectors (tech/pharma)
                      negative tone → crackdown (compliance wins)
                      positive tone → REGULATORY_RELAXING (tech/pharma win)
    """
    if event_type == 'ENERGY':
        momentum = _get_sector_avg_momentum(['Oil & Gas', 'Energy'])
        if momentum < -1.0:
            log('INFO', 'event_mapper',
                f'ENERGY polarity: BEARISH (sector momentum {momentum:+.1f}%)')
            return 'ENERGY_BEARISH'
        log('INFO', 'event_mapper',
            f'ENERGY polarity: BULLISH (sector momentum {momentum:+.1f}%)')
        return 'ENERGY'

    if event_type == 'MACRO_SHIFT':
        rate_delta = _get_macro_rate_direction()
        if rate_delta < -0.05:
            log('INFO', 'event_mapper',
                f'MACRO_SHIFT polarity: DOVISH (rate delta {rate_delta:+.3f})')
            return 'MACRO_SHIFT_DOVISH'
        log('INFO', 'event_mapper',
            f'MACRO_SHIFT polarity: HAWKISH (rate delta {rate_delta:+.3f})')
        return 'MACRO_SHIFT'

    if event_type == 'HEALTH_CRISIS':
        momentum = _get_sector_avg_momentum(['Healthcare', 'Pharmaceuticals', 'Biotechnology'])
        if momentum < -1.0:
            log('INFO', 'event_mapper',
                f'HEALTH_CRISIS polarity: RESOLVING (pharma momentum {momentum:+.1f}%)')
            return 'HEALTH_CRISIS_RESOLVING'
        log('INFO', 'event_mapper',
            f'HEALTH_CRISIS polarity: WORSENING (pharma momentum {momentum:+.1f}%)')
        return 'HEALTH_CRISIS'

    if event_type == 'GEOPOLITICAL':
        momentum = _get_sector_avg_momentum(['Aerospace & Defense', 'Defense'])
        if momentum < -1.0:
            log('INFO', 'event_mapper',
                f'GEOPOLITICAL polarity: RESOLVING (defense momentum {momentum:+.1f}%)')
            return 'GEOPOLITICAL_RESOLVING'
        log('INFO', 'event_mapper',
            f'GEOPOLITICAL polarity: ESCALATING (defense momentum {momentum:+.1f}%)')
        return 'GEOPOLITICAL'

    if event_type == 'SUPPLY_CHAIN':
        momentum = _get_sector_avg_momentum(['Logistics', 'Shipping'])
        if momentum < -1.0:
            log('INFO', 'event_mapper',
                f'SUPPLY_CHAIN polarity: RESOLVING (logistics momentum {momentum:+.1f}%)')
            return 'SUPPLY_CHAIN_RESOLVING'
        log('INFO', 'event_mapper',
            f'SUPPLY_CHAIN polarity: DISRUPTION (logistics momentum {momentum:+.1f}%)')
        return 'SUPPLY_CHAIN'

    if event_type == 'REGULATORY':
        # For regulatory, we check tone of the regulated sector (tech/pharma).
        # Negative tone → crackdown in progress → compliance wins.
        # Positive tone → relaxation / deregulation signal.
        regulated_stocks = _get_universe_by_sector(['Technology', 'Pharmaceuticals'])
        tones = [_get_gdelt_tone(s['ticker']) for s in regulated_stocks[:15]]
        avg_tone = sum(tones) / len(tones) if tones else 0.0
        if avg_tone > 1.0:
            log('INFO', 'event_mapper',
                f'REGULATORY polarity: RELAXING (regulated sector tone {avg_tone:+.2f})')
            return 'REGULATORY_RELAXING'
        log('INFO', 'event_mapper',
            f'REGULATORY polarity: CRACKDOWN (regulated sector tone {avg_tone:+.2f})')
        return 'REGULATORY'

    return event_type


def _save_candidate(ticker, direction, event_type, reason, base_score):
    execute("""
        INSERT INTO discovery_candidates
            (date, ticker, direction, event_type, reason, total_score, analyzed)
        VALUES (%s, %s, %s, %s, %s, %s, FALSE)
        ON CONFLICT DO NOTHING
    """, (date.today(), ticker, direction, event_type, reason[:500],
          round(base_score, 4)))


# ---------------------------------------------------------------------------
# Main mapper
# ---------------------------------------------------------------------------

def map_events(detected_events):
    """Map each detected event to buy/sell candidates. Returns list of candidates."""
    log('INFO', 'event_mapper', f'Mapping {len(detected_events)} events to stocks...')
    all_candidates = []
    saved = 0

    for event in detected_events:
        raw_event_type = event['event_type']
        confidence = event['confidence']

        # Resolve polarity for bidirectional events using live data
        if raw_event_type in BIDIRECTIONAL_EVENTS:
            event_type = _detect_event_polarity(raw_event_type)
        else:
            event_type = raw_event_type

        sector_map = EVENT_SECTOR_MAP.get(event_type, {})
        poly_confirm = _get_polymarket_confirmation(event_type)
        narrative = EVENT_NARRATIVE.get(event_type, (event_type, '', ''))
        event_title, buy_rationale, sell_rationale = narrative

        # ── BUY candidates ────────────────────────────────────────────────
        buy_sectors = sector_map.get('buy_sectors', [])
        buy_countries = sector_map.get('buy_countries', []) or None
        buy_stocks = _get_universe_by_sector(buy_sectors, buy_countries)

        for stock in buy_stocks[:30]:
            ticker = stock['ticker']
            momentum = _get_momentum_score(ticker)
            velocity = _get_velocity(ticker)
            tone = _get_gdelt_tone(ticker)

            # Per-stock flip: strong reversal against the buy thesis → SELL instead
            if momentum < -MOMENTUM_FLIP_THRESHOLD:
                flipped_reason = (
                    f"{event_title}. {buy_rationale}. "
                    f"Sector: {stock['sector']}. "
                    f"MOMENTUM REVERSAL: price {momentum:+.1f}% despite bullish event — "
                    f"thesis not confirmed, treating as SELL."
                )
                _save_candidate(ticker, 'SELL', event_type, flipped_reason,
                                round(min(1.0, confidence * 0.8), 4))
                all_candidates.append({'ticker': ticker, 'direction': 'SELL',
                                       'event_type': event_type, 'score': confidence * 0.8,
                                       'reason': flipped_reason})
                saved += 1
                continue

            base_score = confidence
            if velocity > 1.5:
                base_score = min(1.0, base_score + 0.10)
            if momentum > MOMENTUM_CONFIRM_THRESHOLD:
                base_score = min(1.0, base_score + 0.05)
            if tone > 1.0:    # positive GDELT tone adds conviction
                base_score = min(1.0, base_score + 0.05)
            if poly_confirm:
                base_score = min(1.0, base_score + 0.10)

            signals = []
            if velocity > 1.5:
                signals.append(f'GDELT {velocity:.1f}x velocity ↑')
            if momentum > MOMENTUM_CONFIRM_THRESHOLD:
                signals.append(f'momentum {momentum:+.1f}%')
            if tone > 1.0:
                signals.append(f'tone {tone:+.1f}')
            if poly_confirm:
                signals.append('Polymarket confirmed')
            signals_str = '; '.join(signals) or f'momentum={momentum:+.1f}%'

            reason = (f"{event_title}. {buy_rationale}. "
                      f"Sector: {stock['sector']}. Signals: {signals_str}.")
            _save_candidate(ticker, 'BUY', event_type, reason, base_score)
            all_candidates.append({'ticker': ticker, 'direction': 'BUY',
                                   'event_type': event_type, 'score': base_score,
                                   'reason': reason})
            saved += 1

        # ── SELL candidates ───────────────────────────────────────────────
        sell_sectors = sector_map.get('sell_sectors', [])
        sell_countries = sector_map.get('sell_countries', []) or None
        sell_stocks = _get_universe_by_sector(sell_sectors, sell_countries)

        for stock in sell_stocks[:20]:
            ticker = stock['ticker']
            momentum = _get_momentum_score(ticker)
            tone = _get_gdelt_tone(ticker)

            # Per-stock flip: strong price recovery on a SELL candidate → thesis already priced in
            if momentum > MOMENTUM_FLIP_THRESHOLD:
                flipped_reason = (
                    f"{event_title}. {sell_rationale}. "
                    f"Sector: {stock['sector']}. "
                    f"MOMENTUM RECOVERY: price {momentum:+.1f}% — bad news priced in, "
                    f"treating as BUY (contrarian)."
                )
                _save_candidate(ticker, 'BUY', event_type, flipped_reason,
                                round(min(1.0, confidence * 0.7), 4))
                all_candidates.append({'ticker': ticker, 'direction': 'BUY',
                                       'event_type': event_type, 'score': confidence * 0.7,
                                       'reason': flipped_reason})
                saved += 1
                continue

            base_score = confidence
            if momentum < -MOMENTUM_CONFIRM_THRESHOLD:
                base_score = min(1.0, base_score + 0.10)
            if tone < -1.0:   # negative tone confirms sell thesis
                base_score = min(1.0, base_score + 0.05)
            if poly_confirm:
                base_score = min(1.0, base_score + 0.10)

            signals = []
            if momentum < -MOMENTUM_CONFIRM_THRESHOLD:
                signals.append(f'price declining {momentum:+.1f}%')
            if tone < -1.0:
                signals.append(f'negative tone {tone:+.1f}')
            if poly_confirm:
                signals.append('Polymarket confirmed')
            signals_str = '; '.join(signals) or f'momentum={momentum:+.1f}%'

            reason = (f"{event_title}. {sell_rationale}. "
                      f"Sector: {stock['sector']}. "
                      f"If held: consider exit. If not held: avoid entry. Signals: {signals_str}.")
            _save_candidate(ticker, 'SELL', event_type, reason, base_score)
            all_candidates.append({'ticker': ticker, 'direction': 'SELL',
                                   'event_type': event_type, 'score': base_score,
                                   'reason': reason})
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
    for c in sorted(candidates, key=lambda x: -x['score'])[:15]:
        print(f"  {c['direction']:4s} {c['ticker']:10s} ({c['event_type']:20s}) score={c['score']:.2f}  {c['reason'][:80]}")
