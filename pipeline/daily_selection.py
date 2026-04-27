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


def _build_diversity_state(already_selected: list, max_holdings: int,
                           rotation_mode: bool = False, target_count: int = 20):
    """
    Build portfolio-aware diversity caps and score adjustments from UI allocation settings.

    Returns (sector_counts, region_counts, sector_cap_fn, region_cap_fn, adjust_score_fn).

    rotation_mode=True (used when portfolio is fully allocated):
      - Overweight penalty disabled — we still need to surface replacement candidates
        from at-cap sectors so reconciliation can evaluate intra-sector rotation.
      - Rotation buffer = round(target_count / max_holdings), so it scales with the
        UI-configured analysis budget and portfolio size rather than being hardcoded.
        e.g. 20 analyses / 10 positions = 2 candidates per position
             40 analyses / 10 positions = 4 candidates per position
             20 analyses / 20 positions = 1 candidate per position
      - Diversification/novelty bonuses kept — cross-sector rotation opportunities
        should still be surfaced.

    Normal mode cap formula:  holdings_in_category + max(1, headroom × 2)
    Rotation mode cap formula: holdings_in_category + max(rotation_buffer, headroom × 2)

    Score adjustment (applied before ranking, preserves within-sector ordering):
      overweight_penalty : up to -0.20 when sector utilisation ≥ 75% (disabled in rotation mode)
      diversification_bonus : up to +0.10 when sector utilisation ≤ 50%
      novelty_bonus : +0.05 when sector not in recommendations last 7 days
      region adjustments: ±0.10 / ±0.05 on same utilisation curves
    """
    # Load UI targets (same DB keys reconciliation reads)
    _st = query("SELECT value FROM user_settings WHERE key='sector_targets'")
    sector_targets: dict = json.loads(_st[0]['value']) if _st and _st[0]['value'] else {}
    _rt = query("SELECT value FROM user_settings WHERE key='region_targets'")
    region_targets: dict = json.loads(_rt[0]['value']) if _rt and _rt[0]['value'] else DEFAULT_REGION_TARGETS

    # Sectors seen in recommendations in the last 7 days (for novelty bonus)
    recent_rows = query("""
        SELECT DISTINCT u.sector
        FROM recommendations r
        JOIN universe u ON u.ticker = r.ticker
        WHERE r.date >= %s AND u.sector IS NOT NULL
    """, (date.today() - timedelta(days=7),))
    recently_analyzed_sectors = {
        (r['sector'] or '').split('-')[0] for r in recent_rows
    }

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

    def _utilisation(held: int, target_slots: int) -> float:
        return held / max(1, target_slots)

    # rotation buffer: scales with analysis budget / portfolio size, min 1
    _buf = max(1, round(target_count / max(1, max_holdings))) if rotation_mode else 1

    def sector_cap(sector: str) -> int:
        tgt_pct  = float(sector_targets.get(sector, 25))
        port_max = max(1, round(max_holdings * tgt_pct / 100))
        h        = port_sector.get(sector, 0)
        return h + max(_buf, (max(0, port_max - h)) * 2)

    def region_cap(region: str) -> int:
        tgt_pct  = float(region_targets.get(region, DEFAULT_REGION_TARGETS.get(region, 40)))
        port_max = max(2, round(max_holdings * tgt_pct / 100))
        h        = port_region.get(region, 0)
        return h + max(_buf, (max(0, port_max - h)) * 2)

    def adjust_score(raw: float, sector: str, region: str) -> float:
        """
        Adjust conviction score for portfolio fit without distorting within-sector ranking.
        All candidates in the same sector+region get the same delta, so the best ticker
        in each category still surfaces first.

        In rotation_mode the overweight penalty is suppressed — we need to see
        replacement candidates from at-cap sectors, not just underweight ones.
        """
        # --- Sector adjustment ---
        s_tgt_pct  = float(sector_targets.get(sector, 25))
        s_slots    = max(1, round(max_holdings * s_tgt_pct / 100))
        s_util     = _utilisation(port_sector.get(sector, 0), s_slots)
        # Penalty suppressed in rotation mode (at-cap sectors still need rotation candidates)
        s_penalty  = 0.0 if rotation_mode else max(0.0, (s_util - 0.75) / 0.25) * 0.20
        # Bonus: linear 0→+0.10 as utilisation goes from 0.50→0.0
        s_bonus    = max(0.0, (0.50 - s_util) / 0.50) * 0.10
        # Novelty: sector not recently in recommendations
        s_novelty  = 0.05 if sector not in recently_analyzed_sectors else 0.0

        # --- Region adjustment (half the magnitude of sector) ---
        r_tgt_pct  = float(region_targets.get(region, DEFAULT_REGION_TARGETS.get(region, 40)))
        r_slots    = max(2, round(max_holdings * r_tgt_pct / 100))
        r_util     = _utilisation(port_region.get(region, 0), r_slots)
        r_penalty  = 0.0 if rotation_mode else max(0.0, (r_util - 0.75) / 0.25) * 0.10
        r_bonus    = max(0.0, (0.50 - r_util) / 0.50) * 0.05

        return raw - s_penalty - r_penalty + s_bonus + r_bonus + s_novelty

    # Seed running counts from ALL already-selected (holdings, watchlist, etc.)
    sector_counts: dict = {}
    region_counts: dict = {}
    for item in already_selected:
        s, c = _sc(item['ticker'])
        sector_counts[s] = sector_counts.get(s, 0) + 1
        r = country_to_region(c)
        region_counts[r] = region_counts.get(r, 0) + 1

    return sector_counts, region_counts, sector_cap, region_cap, adjust_score


def _get_discovery_candidates(exclude: set, already_selected: list = None, max_holdings: int = 10) -> list:
    """
    Return today's BUY discovery candidates ranked by portfolio-adjusted score.

    Candidates are fetched, re-ranked with overweight/diversification/novelty
    adjustments, then filtered by sector/region caps. Within each sector the
    best raw-score candidate always surfaces first (same adjustment delta for
    all members of a sector).
    """
    already_selected = already_selected or []
    sector_counts, region_counts, sector_cap, region_cap, adjust_score = \
        _build_diversity_state(already_selected, max_holdings)

    exc_tuple = tuple(exclude) if exclude else ('__none__',)
    rows = query("""
        SELECT dc.ticker, dc.total_score, u.sector, u.country
        FROM discovery_candidates dc
        LEFT JOIN universe u ON u.ticker = dc.ticker AND u.active = true
        WHERE dc.created_at >= %s AND dc.direction = 'BUY'
        AND dc.ticker NOT IN %s
        ORDER BY dc.total_score DESC LIMIT 30
    """, (date.today() - timedelta(days=1), exc_tuple))

    # Normalise total_score (0-100) to 0-1 range for the adjuster
    candidates = []
    for r in rows:
        sector = (r['sector'] or 'Unknown').split('-')[0]
        region = country_to_region(r['country'] or 'Unknown')
        raw    = float(r['total_score'] or 0) / 100.0
        adj    = adjust_score(raw, sector, region)
        candidates.append((r['ticker'], adj, sector, region))

    candidates.sort(key=lambda x: -x[1])

    result = []
    for t, _adj, sector, region in candidates:
        if sector_counts.get(sector, 0) >= sector_cap(sector):
            continue
        if region_counts.get(region, 0) >= region_cap(region):
            continue
        result.append(t)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        region_counts[region] = region_counts.get(region, 0) + 1

    return result


def _get_universe_rotation_fill(exclude: set, n: int, already_selected: list = None,
                                max_holdings: int = 10, target_count: int = 20,
                                rotation_mode: bool = False) -> list:
    """
    Fill remaining analysis slots from the universe with portfolio-adjusted ranking
    and diversity caps. Within each sector the best conviction ticker still surfaces
    first. Safety top-up fills any remaining gaps without constraints.

    rotation_mode=True when portfolio is fully allocated: penalty suppressed and
    rotation buffer scales with target_count/max_holdings so every held sector
    gets proportional replacement candidates.
    """
    if n <= 0:
        return []

    already_selected = already_selected or []
    sector_counts, region_counts, sector_cap, region_cap, adjust_score = \
        _build_diversity_state(already_selected, max_holdings,
                               rotation_mode=rotation_mode, target_count=target_count)

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

    # Score then re-rank with portfolio adjustments
    scored = []
    for t, s, c in pool[:60]:
        raw = compute_conviction(t)
        region = country_to_region(c)
        adj = adjust_score(raw, s, region)
        scored.append((t, adj, s, region))
    scored.sort(key=lambda x: -x[1])

    result = []
    for t, _adj, sector, region in scored:
        if len(result) >= n:
            break
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
        fill = _get_universe_rotation_fill(seen, shortage, already_selected=selected,
                                           max_holdings=MAX_HOLDINGS, target_count=TARGET_COUNT,
                                           rotation_mode=portfolio_full)
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
