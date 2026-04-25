"""
Signal Engine — computes per-ticker conviction scores from all data sources.
Signals: momentum, sentiment, fundamentals trend, catalyst score, macro alignment,
volatility score, risk flags. Outputs conviction[ticker] in [0, 1].
"""
import sys
from datetime import date, timedelta
from typing import Optional

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log

# Weights for conviction composite
WEIGHTS = {
    'momentum_7d': 0.20,
    'sentiment_avg': 0.25,
    'fundamentals': 0.15,
    'catalyst': 0.15,
    'macro_alignment': 0.10,
    'volatility_penalty': -0.10,
    'multiconfirm': 0.15,
}


def _momentum_score(ticker: str) -> float:
    """7-day price momentum normalised to [0, 1]."""
    rows = query("""
        SELECT close FROM prices
        WHERE ticker = %s AND date >= %s
        ORDER BY date ASC
    """, (ticker, date.today() - timedelta(days=14)))
    if not rows or len(rows) < 2:
        return 0.5
    prices = [float(r['close']) for r in rows if r['close']]
    if len(prices) < 2:
        return 0.5
    pct = (prices[-1] - prices[-7]) / prices[-7] if len(prices) >= 7 else (prices[-1] - prices[0]) / prices[0]
    # Map [-10%, +10%] to [0, 1]
    return max(0.0, min(1.0, 0.5 + pct * 5))


def _sentiment_score(ticker: str) -> float:
    """Composite of GDELT tone + FinBERT scores normalised to [0, 1]."""
    rows = query("""
        SELECT avg_tone, mention_velocity, is_accelerating, tone_trajectory, finbert_confidence
        FROM news_sentiment
        WHERE ticker = %s AND date >= %s
        ORDER BY date DESC LIMIT 7
    """, (ticker, date.today() - timedelta(days=7)))
    if not rows:
        return 0.5
    avg_tone = sum(float(r['avg_tone'] or 0) for r in rows) / len(rows)
    velocity = float(rows[0].get('mention_velocity') or 1.0)
    accelerating = any(r.get('is_accelerating') for r in rows)
    trajectory = float(rows[0].get('tone_trajectory') or 0)
    fb_conf = float(rows[0].get('finbert_confidence') or 0.0)

    # GDELT tone: -10 to +10 range typical → normalise to [0,1]
    # avg_tone is already the FinBERT score×10 when FinBERT ran successfully
    tone_norm = max(0.0, min(1.0, 0.5 + avg_tone / 20))
    vel_bonus = min(0.1, (velocity - 1) * 0.05) if velocity > 1 else 0
    acc_bonus = 0.05 if accelerating else 0
    traj_bonus = max(-0.05, min(0.05, trajectory / 20))

    # Below 0.25 confidence FinBERT is essentially guessing — ignore it and fall
    # back to the raw GDELT col-15 tone (full weight). Above 0.25 scale the tone
    # signal's deviation from neutral proportionally so high confidence preserves
    # the signal and low confidence pulls it toward neutral.
    # Default weight of 0.6 when FinBERT hasn't run at all (fb_conf == 0).
    if fb_conf < 0.25:
        tone_weighted = tone_norm   # treat as raw GDELT tone, no FinBERT adjustment
    else:
        tone_weighted = 0.5 + (tone_norm - 0.5) * fb_conf

    return max(0.0, min(1.0, tone_weighted + vel_bonus + acc_bonus + traj_bonus))


def _fundamentals_score(ticker: str) -> float:
    """Simple fundamentals proxy — uses price stability as surrogate."""
    rows = query("""
        SELECT close FROM prices
        WHERE ticker = %s AND date >= %s
        ORDER BY date ASC
    """, (ticker, date.today() - timedelta(days=30)))
    if not rows or len(rows) < 5:
        return 0.5
    prices = [float(r['close']) for r in rows if r['close']]
    if len(prices) < 5:
        return 0.5
    import statistics
    try:
        cv = statistics.stdev(prices) / statistics.mean(prices)
        return max(0.0, min(1.0, 1 - cv * 5))
    except Exception:
        return 0.5


def _catalyst_score(ticker: str) -> float:
    """Score based on discovery candidates with positive direction."""
    rows = query("""
        SELECT total_score, direction FROM discovery_candidates
        WHERE ticker = %s AND created_at >= %s
        ORDER BY total_score DESC LIMIT 3
    """, (ticker, date.today() - timedelta(days=3)))
    if not rows:
        return 0.5
    buy_scores = [float(r['total_score'] or 0) / 100 for r in rows if r.get('direction') == 'BUY']
    sell_scores = [float(r['total_score'] or 0) / 100 for r in rows if r.get('direction') == 'SELL']
    if buy_scores:
        return max(buy_scores)
    if sell_scores:
        return 1 - max(sell_scores)
    return 0.5


def _macro_alignment_score(ticker: str, country: str) -> float:
    """Check macro environment — positive rates environment, etc."""
    rows = query("""
        SELECT series_id, value FROM macro_data
        WHERE date >= %s ORDER BY date DESC LIMIT 20
    """, (date.today() - timedelta(days=30),))
    if not rows:
        return 0.5
    macro = {r['series_id']: float(r['value'] or 0) for r in rows}
    score = 0.5
    # Rising rates: good for financials, bad for growth
    fed_rate = macro.get('FEDFUNDS', 0)
    inflation = macro.get('CPIAUCSL', 0)
    if fed_rate < 4:
        score += 0.05
    if inflation < 3:
        score += 0.05
    return max(0.0, min(1.0, score))


def _volatility_penalty(ticker: str) -> float:
    """Higher volatility → higher penalty (0 = low vol, 1 = high vol)."""
    rows = query("""
        SELECT close FROM prices
        WHERE ticker = %s AND date >= %s
        ORDER BY date ASC
    """, (ticker, date.today() - timedelta(days=30)))
    if not rows or len(rows) < 5:
        return 0.0
    prices = [float(r['close']) for r in rows if r['close']]
    if len(prices) < 5:
        return 0.0
    import statistics
    try:
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
        vol = statistics.stdev(returns) * (252 ** 0.5)  # annualised
        return max(0.0, min(1.0, vol / 1.0))  # 100% vol = max penalty
    except Exception:
        return 0.0


def _multi_confirm_score(ticker: str) -> float:
    """Bonus when multiple sources agree on direction."""
    signals = []
    rows = query("SELECT mention_velocity FROM news_sentiment WHERE ticker = %s ORDER BY date DESC LIMIT 1", (ticker,))
    if rows and rows[0].get('mention_velocity', 0) and float(rows[0]['mention_velocity']) > 1.5:
        signals.append('gdelt')
    rows = query("SELECT probability FROM polymarket_signals WHERE created_at >= %s ORDER BY created_at DESC LIMIT 3", (date.today() - timedelta(days=1),))
    if rows and any(float(r.get('probability') or 0) > 0.6 for r in rows):
        signals.append('polymarket')
    rows = query("SELECT close FROM prices WHERE ticker = %s AND date >= %s ORDER BY date ASC", (ticker, date.today() - timedelta(days=7)))
    if rows and len(rows) >= 2:
        prices_list = [float(r['close']) for r in rows if r['close']]
        if len(prices_list) >= 2 and prices_list[-1] > prices_list[0]:
            signals.append('price')
    return min(1.0, len(signals) * 0.33)


def compute_conviction(ticker: str, country: str = 'US') -> float:
    """Compute conviction score for a ticker in [0, 1]."""
    momentum = _momentum_score(ticker)
    sentiment = _sentiment_score(ticker)
    fundamentals = _fundamentals_score(ticker)
    catalyst = _catalyst_score(ticker)
    macro = _macro_alignment_score(ticker, country)
    vol_penalty = _volatility_penalty(ticker)
    multi = _multi_confirm_score(ticker)

    raw = (
        WEIGHTS['momentum_7d'] * momentum
        + WEIGHTS['sentiment_avg'] * sentiment
        + WEIGHTS['fundamentals'] * fundamentals
        + WEIGHTS['catalyst'] * catalyst
        + WEIGHTS['macro_alignment'] * macro
        + WEIGHTS['volatility_penalty'] * vol_penalty
        + WEIGHTS['multiconfirm'] * multi
    )
    # Normalise: weights sum to (0.20+0.25+0.15+0.15+0.10+0.15) - 0.10 = 0.90 max positive
    conviction = max(0.0, min(1.0, raw / 0.90))
    return conviction


def compute_signals_batch(tickers: list) -> dict:
    """Compute conviction for multiple tickers. Returns {ticker: conviction}."""
    results = {}
    for t in tickers:
        country_row = query("SELECT country FROM universe WHERE ticker = %s LIMIT 1", (t,))
        country = country_row[0]['country'] if country_row else 'US'
        c = compute_conviction(t, country)
        results[t] = c
        log('signal_engine', 'info', f'{t}: conviction={c:.3f}')
    return results


if __name__ == '__main__':
    import sys
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ['AAPL', 'NVDA', 'NOKIA.HE']
    convictions = compute_signals_batch(tickers)
    print('\nConviction scores:')
    for t, c in sorted(convictions.items(), key=lambda x: -x[1]):
        print(f'  {t}: {c:.3f}')
