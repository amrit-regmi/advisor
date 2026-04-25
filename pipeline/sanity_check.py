"""
Sanity check layer — validates final_actions before delivery.
Runs before 07:00 brief and again at 12:00 (mid-day reverification).

Triggers per ticker:
  - Price move > ±5% since recommendation
  - Volatility spike > 2× 30-day baseline
  - GDELT mention velocity spike (>3× baseline)
  - Polymarket probability shift > 10pp since yesterday
  - GDELT sentiment flip (pos→neg or neg→pos, magnitude > 5)

When a trigger fires:
  - Marks recommendation as NEEDS_REEVALUATION
  - Returns list of flagged tickers with reasons

Only flags a subset — never re-runs TradingAgents globally.
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log

PRICE_MOVE_THRESHOLD = 0.05       # 5%
VOLATILITY_SPIKE_FACTOR = 2.0     # 2× baseline
GDELT_VELOCITY_THRESHOLD = 3.0    # 3× baseline
POLYMARKET_SHIFT_THRESHOLD = 0.10 # 10 percentage points
SENTIMENT_FLIP_MAGNITUDE = 5.0    # GDELT tone units


def _check_price_move(ticker, rec_date):
    """Flag if price moved > ±5% since recommendation date."""
    rows = query("""
        SELECT date, close FROM prices
        WHERE ticker = %s AND date >= %s
        ORDER BY date ASC
    """, (ticker, rec_date))

    if len(rows) < 2:
        return None

    entry_price = float(rows[0]['close'])
    latest_price = float(rows[-1]['close'])
    if entry_price == 0:
        return None

    move = (latest_price - entry_price) / entry_price
    if abs(move) > PRICE_MOVE_THRESHOLD:
        direction = '↑' if move > 0 else '↓'
        return f"Price moved {move:+.1%} {direction} since recommendation"
    return None


def _check_volatility_spike(ticker):
    """Flag if recent volatility > 2× 30-day baseline."""
    rows = query("""
        SELECT close FROM prices
        WHERE ticker = %s
        ORDER BY date DESC LIMIT 35
    """, (ticker,))

    if len(rows) < 10:
        return None

    import numpy as np
    closes = [float(r['close']) for r in rows]
    daily_rets = [(closes[i] - closes[i+1]) / closes[i+1]
                  for i in range(len(closes)-1)]

    recent_vol = float(np.std(daily_rets[:5])) if len(daily_rets) >= 5 else 0
    baseline_vol = float(np.std(daily_rets[5:])) if len(daily_rets) > 5 else recent_vol

    if baseline_vol > 0 and recent_vol > baseline_vol * VOLATILITY_SPIKE_FACTOR:
        ratio = recent_vol / baseline_vol
        return f"Volatility spike: {ratio:.1f}× above 30-day baseline"
    return None


def _check_gdelt_spike(ticker):
    """Flag if GDELT velocity suddenly jumped to > 3× baseline."""
    rows = query("""
        SELECT date, mention_velocity, avg_tone FROM news_sentiment
        WHERE ticker = %s ORDER BY date DESC LIMIT 2
    """, (ticker,))

    if len(rows) < 1:
        return None

    latest_vel = float(rows[0]['mention_velocity'] or 1)
    if latest_vel > GDELT_VELOCITY_THRESHOLD:
        return f"GDELT velocity {latest_vel:.1f}× baseline — news surge detected"
    return None


def _check_sentiment_flip(ticker):
    """Flag if GDELT tone flipped significantly (pos↔neg, magnitude > 5)."""
    rows = query("""
        SELECT date, avg_tone FROM news_sentiment
        WHERE ticker = %s ORDER BY date DESC LIMIT 3
    """, (ticker,))

    if len(rows) < 2:
        return None

    latest_tone = float(rows[0]['avg_tone'] or 0)
    prev_tone = float(rows[1]['avg_tone'] or 0)

    # Flip = sign change AND large magnitude
    if (latest_tone * prev_tone < 0 and
            abs(latest_tone - prev_tone) > SENTIMENT_FLIP_MAGNITUDE):
        direction = 'positive' if latest_tone > 0 else 'negative'
        return f"Sentiment flipped to {direction} (Δtone={latest_tone - prev_tone:+.1f})"
    return None


def _check_polymarket_shift(ticker):
    """Flag if a related Polymarket market shifted > 10pp since yesterday."""
    today_rows = query("""
        SELECT market_id, question, probability FROM polymarket_signals
        WHERE relevant_tickers LIKE %s AND date = %s
    """, (f'%{ticker}%', date.today()))

    yesterday_rows = query("""
        SELECT market_id, probability FROM polymarket_signals
        WHERE relevant_tickers LIKE %s AND date = %s
    """, (f'%{ticker}%', date.today() - timedelta(days=1)))

    if not today_rows or not yesterday_rows:
        return None

    yesterday_by_id = {r['market_id']: float(r['probability'] or 0.5)
                       for r in yesterday_rows}

    for row in today_rows:
        mid = row['market_id']
        prob_now = float(row['probability'] or 0.5)
        prob_prev = yesterday_by_id.get(mid, prob_now)
        shift = abs(prob_now - prob_prev)

        if shift > POLYMARKET_SHIFT_THRESHOLD:
            q = (row['question'] or '')[:60]
            return f"Polymarket shifted {shift:.0%}: '{q}'"
    return None


def check_ticker(ticker, rec_date=None):
    """
    Run all sanity checks for a single ticker.
    Returns list of trigger strings (empty = passes all checks).
    """
    if rec_date is None:
        rec_date = date.today()

    triggers = []
    checks = [
        ('price_move',       _check_price_move(ticker, rec_date)),
        ('volatility_spike', _check_volatility_spike(ticker)),
        ('gdelt_spike',      _check_gdelt_spike(ticker)),
        ('sentiment_flip',   _check_sentiment_flip(ticker)),
        ('polymarket_shift', _check_polymarket_shift(ticker)),
    ]

    for check_name, result in checks:
        if result:
            triggers.append(result)
            log('WARNING', 'sanity_check',
                f'{ticker} [{check_name}]: {result}')

    return triggers


def _mark_needs_reevaluation(ticker, reasons):
    """Update recommendation to flag for re-analysis."""
    execute("""
        UPDATE recommendations
        SET signal_sources = signal_sources || %s
        WHERE ticker = %s AND date = %s AND acted_on = FALSE
    """, (f' [FLAGGED: {"; ".join(reasons[:2])}]', ticker, date.today()))


def run_sanity_checks(final_actions=None):
    """
    Run sanity checks on all unacted recommendations (or provided actions).
    Returns dict: {ticker: [trigger_list]} for flagged tickers only.

    Args:
        final_actions: optional list of dicts with 'ticker' and 'date' keys.
                       If None, reads from today's unacted recommendations.
    """
    log('INFO', 'sanity_check', 'Running sanity checks...')

    if final_actions is None:
        rows = query("""
            SELECT ticker, date FROM recommendations
            WHERE date = %s AND acted_on = FALSE
        """, (date.today(),))
        tickers = [(r['ticker'], r['date']) for r in rows if r['ticker']]
    else:
        tickers = [(a['ticker'], a.get('date', date.today()))
                   for a in final_actions if a.get('ticker')]

    if not tickers:
        log('INFO', 'sanity_check', 'No unacted recommendations to check')
        return {}

    flagged = {}
    passed = 0

    for ticker, rec_date in tickers:
        triggers = check_ticker(ticker, rec_date)
        if triggers:
            flagged[ticker] = triggers
            _mark_needs_reevaluation(ticker, triggers)
        else:
            passed += 1

    log('INFO', 'sanity_check',
        f'Sanity check complete: {passed} passed, {len(flagged)} flagged')

    if flagged:
        for ticker, reasons in flagged.items():
            log('WARNING', 'sanity_check',
                f'{ticker} NEEDS_REEVALUATION: {"; ".join(reasons)}')

    return flagged


def run():
    """Daily pipeline entry point."""
    return run_sanity_checks()


if __name__ == '__main__':
    print('Running sanity checks on today\'s recommendations...')
    flagged = run_sanity_checks()
    if flagged:
        print(f'\n⚠️  {len(flagged)} tickers flagged for re-evaluation:')
        for ticker, reasons in flagged.items():
            print(f'  {ticker}:')
            for r in reasons:
                print(f'    - {r}')
    else:
        print('✅ All recommendations passed sanity checks')
