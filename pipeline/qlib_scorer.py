"""
Qlib scoring layer — ranks discovery candidates using quant factors.
Attempts to use Qlib; falls back to scikit-learn factor model if unavailable.

Factor model (fallback):
  momentum_1m  (25pts): 1-month price return
  momentum_3m  (20pts): 3-month price return
  volatility   (20pts): inverse std-dev of daily returns (lower vol = safer)
  liquidity    (15pts): normalised avg daily volume
  gdelt_vel    (20pts): GDELT mention velocity from news_sentiment

Selects top 10-20 candidates and marks selected_for_analysis=True.
"""
import sys
import numpy as np
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log

# Factor weights (must sum to 100)
WEIGHTS = {
    'momentum_1m': 25,
    'momentum_3m': 20,
    'volatility':  20,
    'liquidity':   15,
    'gdelt_vel':   20,
}

MAX_SELECTED = 20
MIN_SELECTED = 10


def _get_price_features(ticker, as_of: date):
    """Return (mom_1m, mom_3m, volatility, liquidity) or None if insufficient data."""
    rows = query("""
        SELECT date, close, volume FROM prices
        WHERE ticker = %s AND date <= %s
        ORDER BY date DESC LIMIT 65
    """, (ticker, as_of))

    if len(rows) < 10:
        return None

    closes = [float(r['close']) for r in rows]
    volumes = [float(r['volume'] or 0) for r in rows]

    latest = closes[0]
    mom_1m = (latest / closes[min(21, len(closes)-1)] - 1) if len(closes) >= 5 else 0.0
    mom_3m = (latest / closes[min(63, len(closes)-1)] - 1) if len(closes) >= 20 else 0.0

    # Daily returns for volatility
    daily_rets = [
        (closes[i] - closes[i+1]) / closes[i+1]
        for i in range(min(30, len(closes)-1))
    ]
    volatility = float(np.std(daily_rets)) if daily_rets else 0.1

    avg_vol = float(np.mean(volumes[:20])) if volumes else 0.0

    return mom_1m, mom_3m, volatility, avg_vol


def _get_gdelt_velocity(ticker):
    """Return latest mention_velocity or 1.0 if no data."""
    rows = query("""
        SELECT mention_velocity FROM news_sentiment
        WHERE ticker = %s ORDER BY date DESC LIMIT 1
    """, (ticker,))
    if rows and rows[0]['mention_velocity'] is not None:
        return float(rows[0]['mention_velocity'])
    return 1.0


def _normalize(values, higher_is_better=True):
    """Min-max normalize a list to [0, 1]. Returns list of same length."""
    arr = np.array(values, dtype=float)
    lo, hi = arr.min(), arr.max()
    if hi == lo:
        return [0.5] * len(values)
    normed = (arr - lo) / (hi - lo)
    return normed.tolist() if higher_is_better else (1 - normed).tolist()


def _factor_score(candidates, as_of: date):
    """
    Compute factor scores for each candidate.
    Returns list of dicts with factor exposures and quant_score.
    """
    raw = []
    for c in candidates:
        ticker = c['ticker']
        feats = _get_price_features(ticker, as_of)
        gdelt_vel = _get_gdelt_velocity(ticker)

        if feats is None:
            raw.append({
                'ticker': ticker,
                'direction': c.get('direction', 'BUY'),
                'mom_1m': 0.0,
                'mom_3m': 0.0,
                'volatility': 0.1,
                'liquidity': 0.0,
                'gdelt_vel': gdelt_vel,
                'has_data': False,
            })
        else:
            mom_1m, mom_3m, vol, liq = feats
            raw.append({
                'ticker': ticker,
                'direction': c.get('direction', 'BUY'),
                'mom_1m': mom_1m,
                'mom_3m': mom_3m,
                'volatility': vol,
                'liquidity': liq,
                'gdelt_vel': gdelt_vel,
                'has_data': True,
            })

    if not raw:
        return []

    # Separate BUY and SELL for directional normalization
    # For SELL candidates: negative momentum is good, so invert signs
    def signed(val, direction):
        return val if direction == 'BUY' else -val

    mom1_vals = [signed(r['mom_1m'], r['direction']) for r in raw]
    mom3_vals = [signed(r['mom_3m'], r['direction']) for r in raw]
    vol_vals  = [r['volatility'] for r in raw]
    liq_vals  = [r['liquidity'] for r in raw]
    vel_vals  = [r['gdelt_vel'] for r in raw]

    norm_mom1 = _normalize(mom1_vals, higher_is_better=True)
    norm_mom3 = _normalize(mom3_vals, higher_is_better=True)
    norm_vol  = _normalize(vol_vals,  higher_is_better=False)  # lower vol = better
    norm_liq  = _normalize(liq_vals,  higher_is_better=True)
    norm_vel  = _normalize(vel_vals,  higher_is_better=True)

    scored = []
    for i, r in enumerate(raw):
        quant_score = (
            norm_mom1[i] * WEIGHTS['momentum_1m'] +
            norm_mom3[i] * WEIGHTS['momentum_3m'] +
            norm_vol[i]  * WEIGHTS['volatility']  +
            norm_liq[i]  * WEIGHTS['liquidity']   +
            norm_vel[i]  * WEIGHTS['gdelt_vel']
        )
        scored.append({
            **r,
            'quant_score':        round(quant_score, 2),
            'risk_score':         round(norm_vol[i] * 100, 2),
            'liquidity_score':    round(norm_liq[i] * 100, 2),
            'momentum_score':     round((norm_mom1[i] + norm_mom3[i]) / 2 * 100, 2),
            'regime_adj_score':   round(quant_score, 2),
        })

    return sorted(scored, key=lambda x: -x['quant_score'])


def _try_qlib(candidates, as_of: date):
    """
    Attempt Qlib offline scoring.
    Returns ranked list or None if Qlib unavailable.
    """
    try:
        import qlib  # noqa: F401
        log('INFO', 'qlib_scorer', 'Qlib available but custom handler not yet wired — using factor fallback')
        return None
    except ImportError:
        return None


def rank_candidates(candidates=None, as_of: date = None):
    """
    Rank discovery candidates by quant factors.
    Selects top MIN_SELECTED–MAX_SELECTED for TradingAgents analysis.

    Returns list of dicts with quant scores and selected_flag.
    """
    if as_of is None:
        as_of = date.today()

    if candidates is None:
        rows = query("""
            SELECT ticker, direction, event_type, total_score, reason,
                   currency, fx_risk, tax_notes, eligible
            FROM discovery_candidates
            WHERE date = %s AND eligible = TRUE
            ORDER BY total_score DESC
        """, (as_of,))
        candidates = [dict(r) for r in rows]

    if not candidates:
        log('INFO', 'qlib_scorer', 'No candidates to rank')
        return []

    log('INFO', 'qlib_scorer',
        f'Ranking {len(candidates)} candidates with quant factors...')

    # Try Qlib first, fall back to factor model
    ranked = _try_qlib(candidates, as_of)
    if ranked is None:
        log('INFO', 'qlib_scorer', 'Using scikit-learn factor model (Qlib not available)')
        ranked = _factor_score(candidates, as_of)

    # Blend discovery total_score (from scorer.py) with quant_score
    cand_by_ticker = {c['ticker']: c for c in candidates}
    for r in ranked:
        discovery_score = float(cand_by_ticker.get(r['ticker'], {}).get('total_score', 50) or 50)
        # 60% discovery signal + 40% quant factor
        r['blended_score'] = round(discovery_score * 0.6 + r['quant_score'] * 0.4, 2)

    ranked.sort(key=lambda x: -x['blended_score'])

    # Select top N
    n_select = max(MIN_SELECTED, min(MAX_SELECTED, len(ranked)))
    for i, r in enumerate(ranked):
        r['selected_flag'] = i < n_select

    # Persist scores back to discovery_candidates
    for r in ranked:
        execute("""
            UPDATE discovery_candidates
            SET blended_score          = %s,
                quant_score            = %s,
                selected_for_analysis  = %s
            WHERE ticker = %s AND date = %s
        """, (
            r['blended_score'],
            r['quant_score'],
            r['selected_flag'],
            r['ticker'],
            as_of,
        ))

    selected = [r for r in ranked if r['selected_flag']]
    log('INFO', 'qlib_scorer',
        f'Selected {len(selected)}/{len(ranked)} candidates. '
        f'Top: {", ".join(r["ticker"] for r in selected[:5])}')

    return ranked


def run():
    """Entry point for daily pipeline."""
    log('INFO', 'qlib_scorer', 'Starting Qlib/factor scoring...')
    ranked = rank_candidates()
    if ranked:
        top = [r for r in ranked if r['selected_flag']]
        log('INFO', 'qlib_scorer',
            f'Qlib scoring complete: {len(top)} selected for analysis')
        for r in top[:10]:
            log('INFO', 'qlib_scorer',
                f"  {r['direction']:4s} {r['ticker']:10s} "
                f"blended={r['blended_score']:.1f} "
                f"quant={r['quant_score']:.1f} "
                f"mom={r.get('momentum_score', 0):.1f} "
                f"risk={r.get('risk_score', 0):.1f}")
    return ranked


if __name__ == '__main__':
    results = run()
    print(f'\n--- Qlib/Factor Scoring Results ---')
    print(f'Total ranked: {len(results)}')
    selected = [r for r in results if r.get('selected_flag')]
    print(f'Selected for analysis: {len(selected)}')
    for r in selected[:20]:
        print(f"  {'✓' if r['selected_flag'] else ' '} "
              f"{r['direction']:4s} {r['ticker']:10s} "
              f"blended={r['blended_score']:5.1f} "
              f"quant={r['quant_score']:5.1f} "
              f"[{'has data' if r.get('has_data') else 'no price data'}]")
