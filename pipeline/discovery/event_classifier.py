"""
Event classifier — reads GDELT trend data and Polymarket to detect
financial events with confidence scores.
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log

EVENT_TYPES = {
    'HEALTH_CRISIS': {
        'gdelt_themes': ['HEALTH_PANDEMIC', 'HEALTH_DISEASE', 'MED_MEDICATION'],
        'keywords': ['vaccine', 'pandemic', 'outbreak', 'fda', 'trial'],
        'cameo_codes': ['204', '205'],
        'polymarket_keywords': ['pandemic', 'vaccine', 'covid', 'disease', 'fda'],
    },
    'GEOPOLITICAL': {
        'gdelt_themes': ['MILITARY', 'SANCTION', 'UNGP_PEACESEC'],
        'keywords': ['war', 'invasion', 'sanctions', 'military', 'conflict'],
        'cameo_codes': ['190', '191', '192', '173'],
        'polymarket_keywords': ['war', 'invasion', 'ukraine', 'russia', 'china', 'taiwan', 'sanctions'],
    },
    'TECH_BREAKTHROUGH': {
        'gdelt_themes': ['TECH', 'SCIENCE_COMPUTING'],
        'keywords': ['ai', 'chip', 'semiconductor', 'quantum', 'breakthrough'],
        'cameo_codes': [],
        'polymarket_keywords': ['ai', 'semiconductor', 'nvidia', 'chip', 'openai'],
    },
    'MACRO_SHIFT': {
        'gdelt_themes': ['ECON_BANKRUPTCY', 'TAX_FNCACT', 'WB_696_FINANCE'],
        'keywords': ['fed', 'rate', 'inflation', 'recession', 'gdp', 'central bank'],
        'cameo_codes': [],
        'polymarket_keywords': ['fed', 'rate cut', 'rate hike', 'inflation', 'recession', 'gdp'],
    },
    'ENERGY': {
        'gdelt_themes': ['ENV_OIL', 'ENV_COAL', 'ENV_NUCLEAR'],
        'keywords': ['oil', 'gas', 'opec', 'pipeline', 'energy', 'crude'],
        'cameo_codes': [],
        'polymarket_keywords': ['oil', 'opec', 'energy', 'crude', 'natural gas'],
    },
    'SUPPLY_CHAIN': {
        'gdelt_themes': ['WB_2105_TRANSPORT', 'ECON_TRADE'],
        'keywords': ['shortage', 'disruption', 'logistics', 'port', 'chip shortage'],
        'cameo_codes': [],
        'polymarket_keywords': ['shortage', 'supply chain', 'tariff', 'trade war'],
    },
    'REGULATORY': {
        'gdelt_themes': ['UNGP_BUSINESS', 'ECON_TAXATION'],
        'keywords': ['antitrust', 'regulation', 'ban', 'lawsuit', 'fine', 'sec'],
        'cameo_codes': ['172', '173'],
        'polymarket_keywords': ['antitrust', 'regulation', 'ban', 'sec', 'lawsuit'],
    },
}

MIN_CONFIDENCE = 0.4


def _score_from_gdelt(event_type, config):
    """Score based on accelerating GDELT themes matching event type."""
    theme_targets = config['gdelt_themes']
    keyword_targets = config['keywords']

    # Get accelerating rows from last 2 days
    accel_rows = query("""
        SELECT ticker, top_themes, mention_velocity, tone_trajectory, avg_tone
        FROM news_sentiment
        WHERE date >= %s AND is_accelerating = TRUE
        ORDER BY mention_velocity DESC
        LIMIT 100
    """, (date.today() - timedelta(days=2),))

    # Also get high-velocity rows even if not flagged accelerating
    velocity_rows = query("""
        SELECT ticker, top_themes, mention_velocity, tone_trajectory, avg_tone
        FROM news_sentiment
        WHERE date >= %s AND mention_velocity >= 1.3
        ORDER BY mention_velocity DESC
        LIMIT 100
    """, (date.today() - timedelta(days=2),))

    # Get all recent rows for keyword scoring
    all_today = query("""
        SELECT ticker, top_themes, article_count, mention_velocity
        FROM news_sentiment
        WHERE date = %s AND article_count > 0
    """, (date.today(),))

    theme_score = 0.0
    keyword_score = 0.0
    matching_tickers = []

    all_rows = {r['ticker']: r for r in velocity_rows}
    for r in accel_rows:
        all_rows[r['ticker']] = r  # accel overrides

    for row in all_rows.values():
        themes_str = (row['top_themes'] or '').upper()
        velocity = float(row['mention_velocity'] or 1)
        # Weight accelerating tickers more
        weight = min(2.0, velocity) * (1.5 if row in accel_rows else 1.0)

        theme_hits = sum(1 for t in theme_targets if t in themes_str)
        if theme_hits > 0:
            contribution = min(0.4, theme_hits * 0.2 * weight)
            theme_score = min(1.0, theme_score + contribution)
            matching_tickers.append(row['ticker'])

    # Keyword scoring against all today's themes
    for row in all_today:
        themes_str = (row['top_themes'] or '').upper()
        velocity = float(row['mention_velocity'] or 1)
        for kw in keyword_targets:
            if kw.upper() in themes_str:
                keyword_score = min(1.0, keyword_score + 0.15 * min(2.0, velocity))
        # Also check theme names contain any keyword fragments
        for t in theme_targets:
            if t in themes_str:
                keyword_score = min(1.0, keyword_score + 0.1)

    combined = (theme_score * 0.65 + keyword_score * 0.35)
    return min(1.0, combined), list(set(matching_tickers))


def _score_from_polymarket(event_type, config):
    """Score based on Polymarket market probabilities matching event type."""
    poly_keywords = config['polymarket_keywords']

    markets = query("""
        SELECT question, probability
        FROM polymarket_signals
        WHERE date >= %s
        ORDER BY date DESC
        LIMIT 200
    """, (date.today() - timedelta(days=1),))

    score = 0.0
    matched_markets = []

    for market in markets:
        q = (market['question'] or '').lower()
        prob = float(market['probability'] or 0.5)

        hits = sum(1 for kw in poly_keywords if kw.lower() in q)
        if hits > 0:
            # High or low probability both signal events (>70% or <30%)
            prob_signal = abs(prob - 0.5) * 2  # 0=neutral, 1=extreme
            score = min(1.0, score + hits * 0.1 * (0.5 + prob_signal))
            matched_markets.append(f"{q[:50]} ({prob:.0%})")

    return min(1.0, score), matched_markets[:5]


def classify_events():
    """Detect and score all event types. Returns list of (event_type, confidence, details)."""
    log('INFO', 'event_classifier', 'Classifying events from GDELT + Polymarket...')
    detected = []

    for event_type, config in EVENT_TYPES.items():
        gdelt_score, gdelt_tickers = _score_from_gdelt(event_type, config)
        poly_score, poly_markets = _score_from_polymarket(event_type, config)

        # Combined confidence: GDELT 60%, Polymarket 40%
        confidence = round(gdelt_score * 0.6 + poly_score * 0.4, 4)

        if confidence >= MIN_CONFIDENCE:
            details = {
                'event_type': event_type,
                'confidence': confidence,
                'gdelt_score': round(gdelt_score, 4),
                'poly_score': round(poly_score, 4),
                'gdelt_tickers': gdelt_tickers,
                'poly_markets': poly_markets,
            }
            detected.append(details)
            log('INFO', 'event_classifier',
                f'{event_type}: confidence={confidence:.2f} '
                f'(gdelt={gdelt_score:.2f}, poly={poly_score:.2f})')

            # Save to system_logs
            execute("""
                INSERT INTO system_logs (level, component, message)
                VALUES ('EVENT', 'event_classifier', %s)
            """, (f"{event_type}|confidence={confidence:.4f}|"
                  f"gdelt_tickers={','.join(gdelt_tickers[:5])}",))

    if not detected:
        log('INFO', 'event_classifier', 'No events detected above threshold')
    else:
        log('INFO', 'event_classifier',
            f'Detected {len(detected)} event(s): '
            f'{", ".join(e["event_type"] for e in detected)}')

    return detected


if __name__ == '__main__':
    events = classify_events()
    print(f'\n--- Detected Events ({len(events)}) ---')
    for e in sorted(events, key=lambda x: -x['confidence']):
        print(f"  {e['event_type']}: {e['confidence']:.2f} confidence")
        if e['gdelt_tickers']:
            print(f"    GDELT tickers: {', '.join(e['gdelt_tickers'][:5])}")
        if e['poly_markets']:
            print(f"    Polymarket: {e['poly_markets'][0]}")
