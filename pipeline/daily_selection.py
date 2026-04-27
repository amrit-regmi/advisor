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
import json
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')
from db.database import query, log, get_setting
from pipeline.signal_engine import compute_conviction
from portfolio.sector_utils import country_to_region, DEFAULT_REGION_TARGETS

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


def _build_diversity_state(already_selected: list, max_holdings: int):
    """
    Build portfolio-aware diversity caps from UI allocation settings.

    Returns (sector_counts, region_counts, sector_cap_fn, region_cap_fn) where
    the cap functions tell you the maximum number of analysis slots allowed for
    a given sector/region given current portfolio headroom.

    Cap formula:  holdings_in_category + max(1, headroom × 2)
    headroom    = UI-configured max slots − current holding count
    """
    # Load UI targets (same DB keys reconciliation reads)
    _st = query("SELECT value FROM user_settings WHERE key='sector_targets'")
    sector_targets: dict = json.loads(_st[0]['value']) if _st and _st[0]['value'] else {}
    _rt = query("SELECT value FROM user_settings WHERE key='region_targets'")
    region_targets: dict = json.loads(_rt[0]['value']) if _rt and _rt[0]['value'] else DEFAULT_REGION_TARGETS

    # Fetch sector/country for all already-selected tickers in one query
    all_tickers = [item['ticker'] for item in already_selected]
    meta_map: dict = {}
    if all_tickers:
        rows = query("SELECT ticker, sector, country FROM universe WHERE ticker IN %s", (tuple(all_tickers),))
        meta_map = {r['ticker']: r for r in rows}

    def _sc(ticker: str) -> tuple:
        r = meta_map.get(ticker, {})
        return (
            (r.get('sector') or 'Unknown').split('-')[0],  # normalise ETF-* → ETF
            r.get('country') or 'Unknown',
        )

    # Portfolio headroom: driven by holdings only
    port_sector: dict = {}
    port_region: dict = {}
    for item in already_selected:
        if item.get('state') == 'holding':
            s, c = _sc(item['ticker'])
            port_sector[s] = port_sector.get(s, 0) + 1
            port_region[country_to_region(c)] = port_region.get(country_to_region(c), 0) + 1

    def sector_cap(sector: str) -> int:
        tgt_pct  = float(sector_targets.get(sector, 25))
        port_max = max(1, round(max_holdings * tgt_pct / 100))
        h        = port_sector.get(sector, 0)
        return h + max(1, (max(0, port_max - h)) * 2)

    def region_cap(region: str) -> int:
        tgt_pct  = float(region_targets.get(region, DEFAULT_REGION_TARGETS.get(region, 40)))
        port_max = max(2, round(max_holdings * tgt_pct / 100))
        h        = port_region.get(region, 0)
        return h + max(1, (max(0, port_max - h)) * 2)

    # Seed running counts from ALL already-selected (holdings, watchlist, etc.)
    sector_counts: dict = {}
    region_counts: dict = {}
    for item in already_selected:
        s, c = _sc(item['ticker'])
        sector_counts[s] = sector_counts.get(s, 0) + 1
        r = country_to_region(c)
        region_counts[r] = region_counts.get(r, 0) + 1

    return sector_counts, region_counts, sector_cap, region_cap


def _get_discovery_candidates(exclude: set, already_selected: list = None, max_holdings: int = 10) -> list:
    """
    Return today's BUY discovery candidates filtered by portfolio-aware diversity caps.
    Fetches top-scored candidates and skips any that would exceed sector/region headroom.
    """
    already_selected = already_selected or []
    sector_counts, region_counts, sector_cap, region_cap = _build_diversity_state(already_selected, max_holdings)

    exc_tuple = tuple(exclude) if exclude else ('__none__',)
    rows = query("""
        SELECT dc.ticker, u.sector, u.country
        FROM discovery_candidates dc
        LEFT JOIN universe u ON u.ticker = dc.ticker AND u.active = true
        WHERE dc.created_at >= %s AND dc.direction = 'BUY'
        AND dc.ticker NOT IN %s
        ORDER BY dc.total_score DESC LIMIT 20
    """, (date.today() - timedelta(days=1), exc_tuple))

    result = []
    for r in rows:
        t      = r['ticker']
        sector = (r['sector'] or 'Unknown').split('-')[0]
        region = country_to_region(r['country'] or 'Unknown')
        if sector_counts.get(sector, 0) >= sector_cap(sector):
            continue
        if region_counts.get(region, 0) >= region_cap(region):
            continue
        result.append(t)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        region_counts[region] = region_counts.get(region, 0) + 1

    return result


def _get_universe_rotation_fill(exclude: set, n: int, already_selected: list = None, max_holdings: int = 10) -> list:
    """
    Fill remaining analysis slots from the universe with portfolio-aware diversity caps.
    Uses the same cap logic as discovery so the full pipeline is consistent.
    Safety top-up fills any remaining gaps without constraints.
    """
    if n <= 0:
        return []

    already_selected = already_selected or []
    sector_counts, region_counts, sector_cap, region_cap = _build_diversity_state(already_selected, max_holdings)

    exc_tuple = tuple(exclude) if exclude else ('__none__',)
    exc_fragment, exc_params = _EXCHANGE_FILTER
    rows = query(
        f"""SELECT u.ticker, u.sector, u.country
            FROM universe u
            WHERE u.active = true AND u.ticker NOT IN %s{exc_fragment}
            ORDER BY RANDOM() LIMIT %s""",
        (exc_tuple, *exc_params, n * 6),
    )

    pool = [(r['ticker'], (r['sector'] or 'Unknown').split('-')[0], r['country'] or 'Unknown')
            for r in rows]
    scored = sorted(
        ((t, compute_conviction(t), s, c) for t, s, c in pool[:60]),
        key=lambda x: -x[1],
    )

    result = []
    for t, _conv, sector, country in scored:
        if len(result) >= n:
            break
        region = country_to_region(country)
        if sector_counts.get(sector, 0) >= sector_cap(sector):
            continue
        if region_counts.get(region, 0) >= region_cap(region):
            continue
        result.append(t)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        region_counts[region] = region_counts.get(region, 0) + 1

    # Safety: if caps left unfilled slots, top-up without constraints
    if len(result) < n:
        used = set(result) | exclude
        for t, _conv, _s, _c in scored:
            if t not in used:
                result.append(t)
                used.add(t)
            if len(result) >= n:
                break

    return result


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
        discovery = _get_discovery_candidates(seen, already_selected=selected, max_holdings=MAX_HOLDINGS)
        for t in discovery[:SLOTS['discovery']]:
            if t not in seen:
                c = compute_conviction(t)
                selected.append({'ticker': t, 'state': 'discovery', 'conviction': c})
                seen.add(t)
        log('daily_selection', 'info', f'Discovery: {min(len(discovery), SLOTS["discovery"])} added')

    # 5. Rotation fill to reach TARGET_COUNT — diversity-aware
    shortage = TARGET_COUNT - len(selected)
    if shortage > 0:
        fill = _get_universe_rotation_fill(seen, shortage, already_selected=selected, max_holdings=MAX_HOLDINGS)
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
