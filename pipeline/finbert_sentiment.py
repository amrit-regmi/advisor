"""
FinBERT sentiment engine — scores news headlines using ProsusAI/finbert.
Reads GDELT news from news_sentiment table, outputs composite sentiment scores.
Model is loaded once and cached in memory.
"""
import sys
from datetime import date, timedelta
from typing import Optional

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, log

_pipeline = None  # module-level model cache


def _get_pipeline():
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    try:
        from transformers import pipeline
        log('finbert', 'info', 'Loading ProsusAI/finbert model...')
        _pipeline = pipeline(
            'text-classification',
            model='ProsusAI/finbert',
            tokenizer='ProsusAI/finbert',
            top_k=None,
            device=-1,  # CPU
            truncation=True,
            max_length=512,
        )
        log('finbert', 'info', 'FinBERT model loaded')
    except Exception as e:
        log('finbert', 'error', f'Failed to load FinBERT: {e}')
        _pipeline = None
    return _pipeline


def _score_text(text: str) -> Optional[float]:
    """Score a single text. Returns [-1, +1] where +1 = positive, -1 = negative."""
    pipe = _get_pipeline()
    if pipe is None or not text or not text.strip():
        return None
    try:
        results = pipe(text[:512])[0]
        score_map = {r['label'].lower(): r['score'] for r in results}
        positive = score_map.get('positive', 0)
        negative = score_map.get('negative', 0)
        return positive - negative
    except Exception as e:
        log('finbert', 'warn', f'Scoring error: {e}')
        return None


def score_ticker_headlines(ticker: str, days: int = 7) -> dict:
    """
    Pull GDELT summaries for ticker and score with FinBERT.
    Returns dict with finbert_today, finbert_3d_avg, finbert_7d_avg.
    """
    rows = query("""
        SELECT date, avg_tone, top_themes, article_count
        FROM news_sentiment
        WHERE ticker = %s AND date >= %s
        ORDER BY date DESC
    """, (ticker, date.today() - timedelta(days=days)))

    if not rows:
        return {'finbert_today': None, 'finbert_3d_avg': None, 'finbert_7d_avg': None}

    scores = []
    for r in rows:
        # Use GDELT tone as proxy text when no headline available
        themes = r.get('top_themes') or ''
        text = f"{ticker} news: tone {r['avg_tone']:.1f}, themes: {themes[:200]}"
        s = _score_text(text)
        if s is not None:
            scores.append((r['date'], s))

    if not scores:
        return {'finbert_today': None, 'finbert_3d_avg': None, 'finbert_7d_avg': None}

    scores_sorted = sorted(scores, key=lambda x: x[0], reverse=True)
    today_score = scores_sorted[0][1] if scores_sorted else None
    three_day = [s for _, s in scores_sorted[:3]]
    seven_day = [s for _, s in scores_sorted[:7]]

    return {
        'finbert_today': today_score,
        'finbert_3d_avg': sum(three_day) / len(three_day) if three_day else None,
        'finbert_7d_avg': sum(seven_day) / len(seven_day) if seven_day else None,
    }


def run_batch(tickers: list) -> dict:
    """Score multiple tickers. Returns {ticker: sentiment_dict}."""
    results = {}
    for t in tickers:
        results[t] = score_ticker_headlines(t)
        log('finbert', 'info', f'{t}: {results[t]}')
    return results


if __name__ == '__main__':
    import sys
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ['AAPL', 'NVDA']
    results = run_batch(tickers)
    for t, s in results.items():
        print(f'{t}: {s}')
