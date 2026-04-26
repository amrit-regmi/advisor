"""
Daily Selection Pipeline.
Assembles the analysis set from: holdings, watchlist, strong overrides,
discovery candidates, and rotation fill. Feeds into Context Builder and
TradingAgents orchestration.

Configurable via .env:
  DAILY_ANALYSIS_COUNT  — total tickers to analyze per day (default 20)
  MAX_HOLDINGS          — maximum portfolio positions (default 10)
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')
from db.database import query, log, get_setting
from pipeline.signal_engine import compute_conviction

STRONG_CONVICTION_THRESHOLD = 0.70
WATCHLIST_CONVICTION_THRESHOLD = 0.55

SLOTS = {
    'holdings': None,    # all holdings (no cap)
    'watchlist': 3,      # top 3 by conviction
    'strong_override': 3,
    'discovery': 2,
    'rotation_fill': None,  # fills remaining slots to TARGET_COUNT
}


def _analysis_exchange_filter():
    """Return (sql_fragment, params_tuple) for exchange filtering, or ('', ())."""
    raw = get_setting('analysis_markets', os.getenv('ANALYSIS_MARKETS', '')).strip()
    if not raw:
        raw = get_setting('universe_markets', os.getenv('UNIVERSE_MARKETS', '')).strip()
    if not raw:
        return ('', ())
    codes = tuple(c.strip().upper().replace('_', ' ') for c in raw.split(',') if c.strip())
    if not codes:
        return ('', ())
    placeholders = ','.join(['%s'] * len(codes))
    return (f' AND exchange IN ({placeholders})', codes)


_EXCHANGE_FILTER = _analysis_exchange_filter()


def _get_holdings() -> list:
    rows = query("SELECT ticker FROM holdings WHERE active = true AND shares > 0")
    return [r['ticker'] for r in rows]


def _get_watchlist() -> list:
    rows = query("SELECT ticker FROM watchlist WHERE active = true ORDER BY created_at DESC")
    return [r['ticker'] for r in rows]


def _get_discovery_candidates(exclude: set) -> list:
    exc_tuple = tuple(exclude) if exclude else ('__none__',)
    rows = query("""
        SELECT ticker FROM discovery_candidates
        WHERE created_at >= %s AND direction = 'BUY'
        AND ticker NOT IN %s
        ORDER BY total_score DESC LIMIT 10
    """, (date.today() - timedelta(days=1), exc_tuple))
    return [r['ticker'] for r in rows]


def _get_universe_rotation_fill(exclude: set, n: int) -> list:
    if n <= 0:
        return []
    exc_tuple = tuple(exclude) if exclude else ('__none__',)
    exc_fragment, exc_params = _EXCHANGE_FILTER
    # Fetch 3× needed then score — avoids always surfacing the same low-conviction tickers
    rows = query(
        f"SELECT ticker FROM universe WHERE active = true AND ticker NOT IN %s{exc_fragment} ORDER BY RANDOM() LIMIT %s",
        (exc_tuple, *exc_params, n * 3),
    )
    candidates = [r['ticker'] for r in rows]
    scored = sorted(((t, compute_conviction(t)) for t in candidates[:30]), key=lambda x: -x[1])
    return [t for t, _ in scored[:n]]


def build_daily_selection() -> list:
    """
    Returns list of {ticker, state, conviction} dicts up to DAILY_ANALYSIS_COUNT entries.
    """
    MAX_HOLDINGS = get_setting('max_holdings', int(os.getenv('MAX_HOLDINGS', 10)))
    TARGET_COUNT = get_setting('daily_analysis_count', int(os.getenv('DAILY_ANALYSIS_COUNT', 20)))
    selected = []
    seen = set()

    # 1. All holdings
    holdings = _get_holdings()
    for t in holdings:
        if t not in seen:
            c = compute_conviction(t)
            selected.append({'ticker': t, 'state': 'holding', 'conviction': c})
            seen.add(t)
    log('daily_selection', 'info', f'Holdings: {len(holdings)} tickers')

    # When at max positions skip discovery — save LLM quota for names we can actually trade
    portfolio_full = len(holdings) >= MAX_HOLDINGS

    # 2. Watchlist top-3 by conviction
    watchlist = _get_watchlist()
    wl_scored = sorted(
        ((t, compute_conviction(t)) for t in watchlist if t not in seen),
        key=lambda x: -x[1],
    )
    for t, c in wl_scored[:SLOTS['watchlist']]:
        selected.append({'ticker': t, 'state': 'watchlist', 'conviction': c})
        seen.add(t)
    log('daily_selection', 'info', f'Watchlist: {min(len(wl_scored), SLOTS["watchlist"])} added')

    # 3. Strong conviction overrides (conviction >= 0.70, not already held)
    if SLOTS['strong_override'] > 0:
        seen_tuple = tuple(seen) if seen else ('__none__',)
        exc_fragment, exc_params = _EXCHANGE_FILTER
        universe_rows = query(
            f"SELECT ticker FROM universe WHERE active = true AND ticker NOT IN %s{exc_fragment} LIMIT 200",
            (seen_tuple, *exc_params),
        )
        strong_scored = sorted(
            ((r['ticker'], compute_conviction(r['ticker'])) for r in universe_rows),
            key=lambda x: -x[1],
        )
        strong_scored = [(t, c) for t, c in strong_scored if c >= STRONG_CONVICTION_THRESHOLD]
        for t, c in strong_scored[:SLOTS['strong_override']]:
            if t not in seen:
                selected.append({'ticker': t, 'state': 'strong_override', 'conviction': c})
                seen.add(t)
        log('daily_selection', 'info', f'Strong overrides: {min(len(strong_scored), SLOTS["strong_override"])} added')

    # 4. Discovery candidates (only when portfolio has room)
    if not portfolio_full:
        discovery = _get_discovery_candidates(seen)
        for t in discovery[:SLOTS['discovery']]:
            if t not in seen:
                c = compute_conviction(t)
                selected.append({'ticker': t, 'state': 'discovery', 'conviction': c})
                seen.add(t)
        log('daily_selection', 'info', f'Discovery: {min(len(discovery), SLOTS["discovery"])} added')

    # 5. Rotation fill to reach TARGET_COUNT
    shortage = TARGET_COUNT - len(selected)
    if shortage > 0:
        fill = _get_universe_rotation_fill(seen, shortage)
        for t in fill:
            if t not in seen:
                c = compute_conviction(t)
                selected.append({'ticker': t, 'state': 'rotation_fill', 'conviction': c})
                seen.add(t)
        log('daily_selection', 'info', f'Rotation fill: {len(fill)} added')

    log('daily_selection', 'info', f'Selection built: {len(selected)} tickers (target={TARGET_COUNT}, max_holdings={MAX_HOLDINGS})')
    for item in selected:
        log('daily_selection', 'info', f"  {item['ticker']} ({item['state']}) conviction={item['conviction']:.3f}")

    return selected[:TARGET_COUNT]


# Backward-compat alias used by trading_agents_wrapper
build_daily_sixteen = build_daily_selection


if __name__ == '__main__':
    result = build_daily_selection()
    print(f'\nDaily selection ({len(result)} tickers):')
    for item in result:
        print(f"  {item['ticker']:12s} {item['state']:15s} {item['conviction']:.3f}")
