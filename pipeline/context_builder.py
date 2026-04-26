"""
Context Builder — assembles per-ticker context for TradingAgents injection.
Pulls signals, sentiment, price data, portfolio state, discovery/watchlist metadata.
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, log
from pipeline.signal_engine import compute_conviction
from pipeline.finbert_sentiment import score_ticker_headlines


def _get_signals(ticker: str) -> dict:
    from pipeline.signal_engine import (
        _momentum_score, _sentiment_score, _fundamentals_score,
        _catalyst_score, _macro_alignment_score, _volatility_penalty,
        _multi_confirm_score
    )
    country_row = query("SELECT country FROM universe WHERE ticker = %s LIMIT 1", (ticker,))
    country = country_row[0]['country'] if country_row else 'US'
    return {
        'conviction': compute_conviction(ticker, country),
        'momentum_7d': _momentum_score(ticker),
        'sentiment_avg': _sentiment_score(ticker),
        'fundamentals': _fundamentals_score(ticker),
        'catalyst': _catalyst_score(ticker),
        'macro_alignment': _macro_alignment_score(ticker, country),
        'volatility_penalty': _volatility_penalty(ticker),
        'multi_confirm': _multi_confirm_score(ticker),
    }


def _get_discovery_metadata(ticker: str) -> dict:
    row = query("""
        SELECT total_score, direction, event_type, reason, created_at
        FROM discovery_candidates
        WHERE ticker = %s ORDER BY created_at DESC LIMIT 1
    """, (ticker,))
    if not row:
        return {}
    r = row[0]
    return {
        'discovery_score': float(r['total_score'] or 0),
        'direction': r['direction'],
        'event_type': r['event_type'],
        'reason': r['reason'],
    }


def _get_watchlist_metadata(ticker: str) -> dict:
    row = query("SELECT created_at FROM watchlist WHERE ticker = %s AND active = true LIMIT 1", (ticker,))
    if not row:
        return {}
    r = row[0]
    days_on = (date.today() - r['created_at'].date()).days if r.get('created_at') else 0
    return {'days_on_watchlist': days_on}


def _get_setting(key: str, default=0):
    row = query("SELECT value FROM user_settings WHERE key = %s LIMIT 1", (key,))
    if row:
        try:
            return float(row[0]['value'])
        except Exception:
            return default
    return default


def _get_portfolio_snapshot() -> dict:
    holdings = query("SELECT ticker, shares, avg_buy_price FROM holdings WHERE active = true AND shares > 0")
    cash_eur = _get_setting('nordnet_cash_eur', 0)
    simulate_on = query("SELECT value FROM user_settings WHERE key='simulate_recommendations'")
    simulate_on = (simulate_on[0]['value'] or '').lower() == 'true' if simulate_on else False
    if simulate_on:
        monthly_budget_row = query("SELECT value FROM user_settings WHERE key='simulation_monthly_deposit'")
        monthly_budget = float(monthly_budget_row[0]['value']) if monthly_budget_row else _get_setting('monthly_investment_budget_eur', 500)
    else:
        monthly_budget = _get_setting('monthly_investment_budget_eur', 500)
    monthly_invested = _get_setting('monthly_invested_this_month', 0)
    budget_left = max(0, monthly_budget - monthly_invested)
    sectors = {}
    countries = {}
    for h in holdings:
        u = query("SELECT sector, country FROM universe WHERE ticker = %s AND active = true LIMIT 1", (h['ticker'],))
        if u:
            sec = u[0].get('sector') or 'Unknown'
            ctry = u[0].get('country') or 'Unknown'
            sectors[sec] = sectors.get(sec, 0) + 1
            countries[ctry] = countries.get(ctry, 0) + 1
    return {
        'n_holdings': len(holdings),
        'holdings': [r['ticker'] for r in holdings],
        'cash_eur': cash_eur,
        'monthly_budget_eur': monthly_budget,
        'budget_left_eur': budget_left,
        'monthly_invested_eur': monthly_invested,
        'sector_exposure': sectors,
        'country_exposure': countries,
        'constraints': {
            'max_holdings': 10,
            'max_sector_weight_pct': 25,
            'max_position_weight_pct': 12,
            'cash_min_pct': 5,
        },
    }


def build_context(ticker: str, ticker_state: str) -> dict:
    """
    Build full context dict for one ticker.
    Returns: context[ticker] as spec'd in architecture §10.
    """
    signals = _get_signals(ticker)
    sentiment = score_ticker_headlines(ticker)
    portfolio = _get_portfolio_snapshot()
    discovery_meta = _get_discovery_metadata(ticker)
    watchlist_meta = _get_watchlist_metadata(ticker)
    strong_override = signals['conviction'] >= 0.70

    return {
        'ticker': ticker,
        'ticker_state': ticker_state,
        'signals': signals,
        'conviction': signals['conviction'],
        'sentiment': sentiment,
        'discovery_metadata': discovery_meta,
        'watchlist_metadata': watchlist_meta,
        'strong_override': strong_override,
        'portfolio_snapshot': portfolio,
    }


def build_all_contexts(daily_selection: list) -> dict:
    """Build context for all tickers in the daily selection. Returns {ticker: context}."""
    portfolio = _get_portfolio_snapshot()  # compute once
    contexts = {}
    for item in daily_selection:
        ticker = item['ticker']
        state = item.get('state', 'discovery')
        log('context_builder', 'info', f'Building context for {ticker}')
        try:
            ctx = build_context(ticker, state)
            ctx['portfolio_snapshot'] = portfolio  # share single snapshot
            contexts[ticker] = ctx
        except Exception as e:
            log('context_builder', 'warn', f'{ticker}: context build failed: {e}')
            contexts[ticker] = {
                'ticker': ticker,
                'ticker_state': state,
                'signals': {'conviction': 0.5},
                'conviction': 0.5,
                'sentiment': {},
                'portfolio_snapshot': portfolio,
            }
    return contexts


if __name__ == '__main__':
    import sys
    tickers = [{'ticker': t, 'state': 'watchlist'} for t in (sys.argv[1:] or ['AAPL', 'NVDA'])]
    ctxs = build_all_contexts(tickers)
    for t, c in ctxs.items():
        print(f"\n{t}: conviction={c['conviction']:.3f}, state={c['ticker_state']}")
        print(f"  sentiment: {c['sentiment']}")
        print(f"  signals: {c['signals']}")
