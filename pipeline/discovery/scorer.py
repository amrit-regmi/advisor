"""
Candidate scorer — scores discovery candidates 0-100.
Components: GDELT velocity, tone direction, Polymarket, price momentum, multi-source.
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log
from pipeline.signal_engine import compute_drp


def _score_gdelt_velocity(ticker):
    """0-25 points: mention velocity vs 30-day baseline."""
    row = query("""
        SELECT mention_velocity FROM news_sentiment
        WHERE ticker = %s ORDER BY date DESC LIMIT 1
    """, (ticker,))
    if not row or row[0]['mention_velocity'] is None:
        return 0
    velocity = float(row[0]['mention_velocity'])
    # 1x = 0pts, 2x = 12.5, 3x+ = 25
    return min(25, max(0, (velocity - 1.0) * 12.5))


def _score_gdelt_tone(ticker, direction):
    """0-25 points: tone positivity/negativity + trajectory."""
    row = query("""
        SELECT avg_tone, tone_trajectory FROM news_sentiment
        WHERE ticker = %s ORDER BY date DESC LIMIT 1
    """, (ticker,))
    if not row:
        return 0
    tone = float(row[0]['avg_tone'] or 0)
    trajectory = float(row[0]['tone_trajectory'] or 0)

    if direction == 'BUY':
        # Reward positive tone and improving trajectory
        tone_pts = min(12.5, max(0, tone * 2.5))
        traj_pts = min(12.5, max(0, trajectory * 2.5))
    else:  # SELL
        # Reward negative tone and worsening trajectory
        tone_pts = min(12.5, max(0, -tone * 2.5))
        traj_pts = min(12.5, max(0, -trajectory * 2.5))

    return round(tone_pts + traj_pts, 2)


def _score_polymarket(ticker, event_type):
    """0-20 points: relevant Polymarket probability."""
    # Map event types to financial keywords
    event_keywords = {
        'HEALTH_CRISIS': ['pandemic', 'vaccine', 'disease', 'fda'],
        'GEOPOLITICAL': ['war', 'invasion', 'sanctions', 'conflict'],
        'TECH_BREAKTHROUGH': ['ai', 'semiconductor', 'chip', 'nvidia'],
        'MACRO_SHIFT': ['rate', 'fed', 'recession', 'inflation'],
        'ENERGY': ['oil', 'opec', 'energy', 'crude'],
        'SUPPLY_CHAIN': ['supply chain', 'tariff', 'shortage'],
        'REGULATORY': ['antitrust', 'regulation', 'ban', 'sec'],
    }
    keywords = event_keywords.get(event_type, [])
    ticker_lower = ticker.lower().replace('.', '')

    markets = query("""
        SELECT question, probability, relevant_tickers
        FROM polymarket_signals
        WHERE date >= %s
        ORDER BY date DESC LIMIT 100
    """, (date.today() - timedelta(days=1),))

    best_score = 0.0
    for market in markets:
        q = (market['question'] or '').lower()
        rel_tickers = (market['relevant_tickers'] or '').upper()

        ticker_match = ticker.upper() in rel_tickers
        keyword_match = any(kw in q for kw in keywords)

        if ticker_match or keyword_match:
            prob = float(market['probability'] or 0.5)
            extreme = abs(prob - 0.5) * 2  # 0=neutral, 1=very extreme
            score = extreme * 20
            best_score = max(best_score, score)

    return round(min(20, best_score), 2)


def _score_price_momentum(ticker, direction):
    """0-20 points: price momentum alignment."""
    rows = query("""
        SELECT close FROM prices WHERE ticker = %s
        ORDER BY date DESC LIMIT 30
    """, (ticker,))
    if len(rows) < 5:
        return 0

    latest = float(rows[0]['close'])
    oldest = float(rows[-1]['close'])
    if oldest == 0:
        return 0

    pct = ((latest - oldest) / oldest) * 100

    if direction == 'BUY':
        # Reward positive momentum up to +10%
        pts = min(20, max(0, pct * 2))
    else:  # SELL
        # Reward negative momentum down to -10%
        pts = min(20, max(0, -pct * 2))

    return round(pts, 2)


def _score_multi_source(ticker, event_type):
    """0-10 points: how many independent sources confirm."""
    sources = 0

    # GDELT source
    gdelt = query("""
        SELECT article_count FROM news_sentiment
        WHERE ticker=%s AND date>=%s
    """, (ticker, date.today() - timedelta(days=2)))
    if gdelt and any(int(r['article_count'] or 0) > 0 for r in gdelt):
        sources += 1

    # Polymarket source
    poly = query("""
        SELECT id FROM polymarket_signals
        WHERE date >= %s AND relevant_tickers LIKE %s
    """, (date.today() - timedelta(days=1), f'%{ticker}%'))
    if poly:
        sources += 1

    # Price data source
    price = query("""
        SELECT id FROM prices WHERE ticker=%s AND date>=%s
    """, (ticker, date.today() - timedelta(days=3)))
    if price:
        sources += 1

    return min(10, sources * 3.33)


def score_candidates():
    """Score all unanalyzed candidates and return top 20."""
    log('INFO', 'scorer', 'Scoring discovery candidates...')

    candidates = query("""
        SELECT id, ticker, direction, event_type, reason
        FROM discovery_candidates
        WHERE date = %s AND analyzed = FALSE
    """, (date.today(),))

    if not candidates:
        log('INFO', 'scorer', 'No candidates to score today')
        return []

    scored = []
    for c in candidates:
        ticker = c['ticker']
        direction = c['direction']
        event_type = c['event_type'] or 'UNKNOWN'

        v_score = _score_gdelt_velocity(ticker)
        t_score = _score_gdelt_tone(ticker, direction)
        p_score = _score_polymarket(ticker, event_type)
        m_score = _score_price_momentum(ticker, direction)
        s_score = _score_multi_source(ticker, event_type)

        total = round(v_score + t_score + p_score + m_score + s_score, 2)

        execute("""
            UPDATE discovery_candidates
            SET gdelt_score      = %s,
                polymarket_score = %s,
                momentum_score   = %s,
                total_score      = %s,
                analyzed         = TRUE
            WHERE id = %s
        """, (round(v_score + t_score, 2), p_score, m_score, total, c['id']))

        scored.append({
            'ticker': ticker,
            'direction': direction,
            'event_type': event_type,
            'total_score': total,
            'reason': c['reason'],
            'gdelt_score': round(v_score + t_score, 2),
            'poly_score': p_score,
            'momentum_score': m_score,
        })

    scored.sort(key=lambda x: -x['total_score'])

    # Load sector targets for DRP (score scale 0-100, so alpha=10)
    import json as _j
    _st = query("SELECT value FROM user_settings WHERE key='sector_targets'")
    _sector_targets_raw: dict = _j.loads(_st[0]['value']) if _st and _st[0]['value'] else {}
    target_weights = {s: float(v) / 100 for s, v in _sector_targets_raw.items()}
    N_disc = 50  # discovery pool size for DRP threshold

    # Bulk-fetch sectors for all scored candidates in one query
    all_tickers = [c['ticker'] for c in scored]
    sector_map: dict = {}
    if all_tickers:
        urows = query("SELECT ticker, sector FROM universe WHERE ticker IN %s",
                      (tuple(all_tickers),))
        sector_map = {r['ticker']: (r['sector'] or 'Unknown').split('-')[0] for r in urows}

    # Iterative greedy selection with DRP to avoid sector flooding in top-20
    candidate_sector_counts: dict = {}
    remaining = list(scored)
    top20 = []

    while len(top20) < 20 and remaining:
        best = None
        best_score = float('-inf')
        for c in remaining:
            sec = sector_map.get(c['ticker'], 'Unknown')
            drp = compute_drp(sec, candidate_sector_counts, target_weights,
                              N_disc, alpha=10.0)
            final = c['total_score'] - drp
            if final > best_score:
                best, best_score = c, final
        if best is None:
            break
        top20.append(best)
        remaining.remove(best)
        sec = sector_map.get(best['ticker'], 'Unknown')
        candidate_sector_counts[sec] = candidate_sector_counts.get(sec, 0) + 1

    top_summary = ', '.join(f"{c['ticker']}({c['total_score']:.0f})" for c in top20[:5])
    log('INFO', 'scorer', f'Scored {len(scored)} candidates, top (DRP-adjusted): {top_summary}')
    return top20


if __name__ == '__main__':
    top = score_candidates()
    print(f'\n--- Top scored candidates (before tax filter) ---')
    for c in top[:20]:
        print(f"  {c['direction']:4s} {c['ticker']:8s} score={c['total_score']:5.1f} "
              f"[{c['event_type']}]")
