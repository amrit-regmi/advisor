"""
Daily-16 Selection Pipeline.
Assembles exactly 16 tickers from: holdings, watchlist, strong overrides,
discovery candidates, and rotation fill. Feeds into Context Builder and
TradingAgents orchestration.
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, log
from pipeline.signal_engine import compute_conviction

STRONG_CONVICTION_THRESHOLD = 0.70
WATCHLIST_CONVICTION_THRESHOLD = 0.55
MAX_HOLDINGS = 10
TARGET_COUNT = 20

SLOTS = {
    'holdings': None,        # all holdings
    'watchlist': 3,          # top 3 by conviction
    'strong_override': 3,    # conviction >= 0.70
    'discovery': 2,          # new candidates
    'rotation_fill': None,   # remainder to reach 16
}


def _get_holdings() -> list:
    rows = query("SELECT ticker FROM holdings WHERE active = true AND shares > 0")
    return [r['ticker'] for r in rows]


def _get_watchlist() -> list:
    rows = query("SELECT ticker FROM watchlist WHERE active = true ORDER BY created_at DESC")
    return [r['ticker'] for r in rows]


def _get_discovery_candidates(exclude: set) -> list:
    # PostgreSQL's NOT IN breaks with an empty tuple; use a phantom ticker that will never match
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
    # Fetch 3× the needed count then score, so RANDOM() doesn't always surface the same
    # low-conviction tickers when there are no strong signals in the priority slots.
    rows = query("""
        SELECT ticker FROM universe
        WHERE active = true AND ticker NOT IN %s
        ORDER BY RANDOM() LIMIT %s
    """, (exc_tuple, n * 3))
    candidates = [r['ticker'] for r in rows]
    # Score and sort
    scored = []
    for t in candidates[:30]:
        c = compute_conviction(t)
        scored.append((t, c))
    scored.sort(key=lambda x: -x[1])
    return [t for t, _ in scored[:n]]


def build_daily_sixteen() -> list:
    """
    Returns list of {ticker, state, conviction} dicts — exactly 16 entries
    (or fewer if universe is too small).
    """
    selected = []
    seen = set()

    # 1. All holdings
    holdings = _get_holdings()
    for t in holdings:
        if t not in seen:
            c = compute_conviction(t)
            selected.append({'ticker': t, 'state': 'holding', 'conviction': c})
            seen.add(t)
    log('daily_sixteen', 'info', f'Holdings: {len(holdings)} tickers')

    # When at max positions we can't act on new discoveries anyway, so skip them
    # to save TradingAgents OR quota for names we might actually trade.
    portfolio_full = len(holdings) >= MAX_HOLDINGS

    # 2. Watchlist top-3 by conviction
    watchlist = _get_watchlist()
    wl_scored = [(t, compute_conviction(t)) for t in watchlist if t not in seen]
    wl_scored.sort(key=lambda x: -x[1])
    for t, c in wl_scored[:SLOTS['watchlist']]:
        selected.append({'ticker': t, 'state': 'watchlist', 'conviction': c})
        seen.add(t)
    log('daily_sixteen', 'info', f'Watchlist: {min(len(wl_scored), SLOTS["watchlist"])} added')

    # 3. Strong conviction (non-holdings, conviction >= 0.70)
    remaining_for_strong = SLOTS['strong_override']
    if remaining_for_strong > 0:
        seen_tuple = tuple(seen) if seen else ('__none__',)
        universe_rows = query("SELECT ticker FROM universe WHERE active = true AND ticker NOT IN %s LIMIT 200", (seen_tuple,))
        universe_tickers = [r['ticker'] for r in universe_rows]
        strong_scored = [(t, compute_conviction(t)) for t in universe_tickers]
        strong_scored = [(t, c) for t, c in strong_scored if c >= STRONG_CONVICTION_THRESHOLD]
        strong_scored.sort(key=lambda x: -x[1])
        for t, c in strong_scored[:remaining_for_strong]:
            if t not in seen:
                selected.append({'ticker': t, 'state': 'strong_override', 'conviction': c})
                seen.add(t)
        log('daily_sixteen', 'info', f'Strong overrides: {min(len(strong_scored), remaining_for_strong)} added')

    # 4. Discovery candidates (only if portfolio not full)
    if not portfolio_full:
        discovery = _get_discovery_candidates(seen)
        for t in discovery[:SLOTS['discovery']]:
            if t not in seen:
                c = compute_conviction(t)
                selected.append({'ticker': t, 'state': 'discovery', 'conviction': c})
                seen.add(t)
        log('daily_sixteen', 'info', f'Discovery: {min(len(discovery), SLOTS["discovery"])} added')

    # 5. Rotation fill to reach TARGET_COUNT
    shortage = TARGET_COUNT - len(selected)
    if shortage > 0:
        fill = _get_universe_rotation_fill(seen, shortage)
        for t in fill:
            if t not in seen:
                c = compute_conviction(t)
                selected.append({'ticker': t, 'state': 'rotation_fill', 'conviction': c})
                seen.add(t)
        log('daily_sixteen', 'info', f'Rotation fill: {len(fill)} added')

    log('daily_sixteen', 'info', f'Daily-16 built: {len(selected)} tickers')
    for item in selected:
        log('daily_sixteen', 'info', f"  {item['ticker']} ({item['state']}) conviction={item['conviction']:.3f}")

    return selected[:TARGET_COUNT]


if __name__ == '__main__':
    sixteen = build_daily_sixteen()
    print(f'\nDaily-16 ({len(sixteen)} tickers):')
    for item in sixteen:
        print(f"  {item['ticker']:12s} {item['state']:15s} {item['conviction']:.3f}")
