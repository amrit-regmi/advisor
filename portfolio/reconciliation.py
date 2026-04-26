"""
Global Reconciliation Layer — converts per-ticker TradingAgents decisions
into a portfolio-level trade plan. Applies holdings constraints, sector/country
limits, turnover rules, rotation logic, and watchlist actions.
"""
import os
import sys
import json
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')
from db.database import query, execute, log, get_setting
from portfolio.sector_utils import normalize as normalize_sector, country_to_region, DEFAULT_REGION_TARGETS
MAX_SECTOR_PCT = 25          # fallback if no user target set for that sector
MAX_COUNTRY_PCT = 40
STRONG_CONVICTION_THRESHOLD = 0.70
WATCHLIST_ADD_LOWER = 0.60
WATCHLIST_REMOVE_CONVICTION = 0.55
WATCHLIST_MAX_STAGNATION_DAYS = 14


def _load_sector_targets() -> dict:
    row = query("SELECT value FROM user_settings WHERE key='sector_targets'")
    if row and row[0]['value']:
        try:
            return json.loads(row[0]['value'])
        except Exception:
            pass
    return {}


def _load_region_targets() -> dict:
    row = query("SELECT value FROM user_settings WHERE key='region_targets'")
    if row and row[0]['value']:
        try:
            return json.loads(row[0]['value'])
        except Exception:
            pass
    return DEFAULT_REGION_TARGETS.copy()


def _get_holdings() -> list:
    rows = query("SELECT ticker, shares, avg_buy_price FROM holdings WHERE active = true AND shares > 0")
    return rows or []


def _get_sector_country(ticker: str) -> tuple:
    row = query("SELECT sector, country FROM universe WHERE ticker = %s AND active = true LIMIT 1", (ticker,))
    if row:
        raw_sec = row[0].get('sector') or 'Unknown'
        return normalize_sector(raw_sec), (row[0].get('country') or 'Unknown')
    return 'Unknown', 'Unknown'


def _sector_exposure(holdings: list) -> dict:
    exposure = {}
    for h in holdings:
        sec, _ = _get_sector_country(h['ticker'])
        exposure[sec] = exposure.get(sec, 0) + 1
    return exposure


def _country_exposure(holdings: list) -> dict:
    exposure = {}
    for h in holdings:
        _, ctry = _get_sector_country(h['ticker'])
        exposure[ctry] = exposure.get(ctry, 0) + 1
    return exposure


def _would_breach_sector(ticker: str, holdings: list) -> bool:
    """Block if adding ticker would exceed the user-configured sector target.
    Uses absolute count ceiling derived from sector target %; falls back to
    MAX_SECTOR_PCT if no target is configured for that sector."""
    sec, _ = _get_sector_country(ticker)
    targets = _load_sector_targets()
    # Use user target + 5% tolerance; if sector target is 0% → completely blocked
    tgt_pct = float(targets.get(sec, MAX_SECTOR_PCT))
    if tgt_pct == 0:
        return True  # sector is explicitly excluded
    # Convert to max position count (ceil so first position is always allowed)
    effective_pct = min(tgt_pct + 5, 40)  # 5% tolerance; hard cap at 40%
    max_in_sector = max(1, round(MAX_HOLDINGS * effective_pct / 100))
    exposure = _sector_exposure(holdings)
    return (exposure.get(sec, 0) + 1) > max_in_sector


def _region_exposure(holdings: list) -> dict:
    """Return {region: count} for current holdings."""
    exposure = {}
    for h in holdings:
        _, ctry = _get_sector_country(h['ticker'])
        region = country_to_region(ctry)
        exposure[region] = exposure.get(region, 0) + 1
    return exposure


def _would_breach_country(ticker: str, holdings: list) -> bool:
    """Block if adding ticker would exceed user-configured region target.
    Falls back to MAX_COUNTRY_PCT if no region target is set."""
    region_targets = _load_region_targets()
    _, ctry = _get_sector_country(ticker)
    region = country_to_region(ctry)
    tgt_pct = float(region_targets.get(region, MAX_COUNTRY_PCT))
    effective_pct = min(tgt_pct + 10, 80)  # 10% tolerance for regions
    max_in_region = max(2, round(MAX_HOLDINGS * effective_pct / 100))
    reg_exp = _region_exposure(holdings)
    return (reg_exp.get(region, 0) + 1) > max_in_region


def _update_watchlist_action(ticker: str, action: str, conviction: float):
    try:
        if action == 'add':
            execute("""
                INSERT INTO watchlist (ticker, active, created_at)
                VALUES (%s, true, NOW())
                ON CONFLICT (ticker) DO UPDATE SET active = true
            """, (ticker,))
        elif action == 'remove':
            execute("UPDATE watchlist SET active = false WHERE ticker = %s", (ticker,))
    except Exception as e:
        log('reconciliation', 'warn', f'Watchlist update failed for {ticker}: {e}')


def _apply_watchlist_exit_rules(watchlist_tickers: list, decisions: dict):
    """Remove tickers from watchlist that fail exit criteria."""
    for ticker in watchlist_tickers:
        d = decisions.get(ticker, {})
        conviction = d.get('confidence', 0.5)
        action = d.get('action', 'WATCH')

        row = query("SELECT created_at FROM watchlist WHERE ticker = %s AND active = true LIMIT 1", (ticker,))
        stagnation = (date.today() - row[0]['created_at'].date()).days if row and row[0].get('created_at') else 0

        should_remove = (
            conviction < WATCHLIST_REMOVE_CONVICTION
            or stagnation > WATCHLIST_MAX_STAGNATION_DAYS
            or action in ('SELL', 'AVOID')
        )
        if should_remove:
            _update_watchlist_action(ticker, 'remove', conviction)
            log('reconciliation', 'info', f'{ticker}: removed from watchlist (conviction={conviction:.2f}, stagnation={stagnation}d)')


def reconcile(decisions: dict, contexts: dict) -> dict:
    """
    Convert per-ticker decisions into trade_plan.
    Returns: {ticker: {action, shares_delta, reasoning, tax_notes, confidence}}
    """
    MAX_HOLDINGS = get_setting('max_holdings', int(os.getenv('MAX_HOLDINGS', 10)))
    holdings = _get_holdings()
    holding_tickers = {h['ticker'] for h in holdings}
    n_holdings = len(holdings)
    portfolio_full = n_holdings >= MAX_HOLDINGS

    watchlist_rows = query("SELECT ticker FROM watchlist WHERE active = true")
    watchlist_tickers = [r['ticker'] for r in watchlist_rows]

    trade_plan = {}
    sells = []
    buys = []
    strong_buys = []

    for ticker, d in decisions.items():
        action = d.get('action', 'WATCH')
        confidence = d.get('confidence', 0.5)
        conviction = contexts.get(ticker, {}).get('conviction', confidence)

        # 13.1 Prioritization: SELL first
        if action == 'SELL':
            if ticker not in holding_tickers:
                log('reconciliation', 'warn', f'{ticker}: SELL ignored — not in holdings')
                continue
            sells.append({'ticker': ticker, 'confidence': confidence, 'decision': d})

        elif action == 'STRONG_BUY':
            strong_buys.append({'ticker': ticker, 'confidence': confidence, 'decision': d})

        elif action == 'BUY':
            buys.append({'ticker': ticker, 'confidence': confidence, 'decision': d})

        elif action == 'WATCH':
            _update_watchlist_action(ticker, 'add', conviction)
            trade_plan[ticker] = {
                'action': 'WATCH',
                'shares_delta': 0,
                'reasoning': d.get('reasoning', ''),
                'confidence': confidence,
            }

        elif action in ('HOLD', 'AVOID'):
            trade_plan[ticker] = {
                'action': action,
                'shares_delta': 0,
                'reasoning': d.get('reasoning', ''),
                'confidence': confidence,
            }

    # Process sells (frees up slots)
    for item in sells:
        ticker = item['ticker']
        trade_plan[ticker] = {
            'action': 'SELL',
            'shares_delta': -1,  # Optimizer determines exact quantity
            'reasoning': item['decision'].get('reasoning', ''),
            'confidence': item['confidence'],
        }
        holding_tickers.discard(ticker)
        n_holdings -= 1
        portfolio_full = n_holdings >= MAX_HOLDINGS

    # Process strong buys
    for item in sorted(strong_buys, key=lambda x: -x['confidence']):
        ticker = item['ticker']
        if ticker in holding_tickers:
            trade_plan[ticker] = {'action': 'HOLD', 'shares_delta': 0, 'reasoning': 'Already held', 'confidence': item['confidence']}
            continue
        if portfolio_full:
            # Check rotation flag
            if item['decision'].get('rotation_flag'):
                # Sell weakest holding
                weakest = _find_weakest_holding(holdings, decisions)
                if weakest:
                    trade_plan[weakest] = {'action': 'SELL', 'shares_delta': -1, 'reasoning': f'Rotated out for {ticker}', 'confidence': 0.6}
                    holding_tickers.discard(weakest)
                    n_holdings -= 1
                    portfolio_full = n_holdings >= MAX_HOLDINGS
            if portfolio_full:
                trade_plan[ticker] = {'action': 'WATCH', 'shares_delta': 0, 'reasoning': 'Portfolio full', 'confidence': item['confidence']}
                _update_watchlist_action(ticker, 'add', item['confidence'])
                continue
        if _would_breach_sector(ticker, [{'ticker': t} for t in holding_tickers]):
            trade_plan[ticker] = {'action': 'WATCH', 'shares_delta': 0, 'reasoning': 'Sector limit breach', 'confidence': item['confidence']}
            _update_watchlist_action(ticker, 'add', item['confidence'])
            continue
        if _would_breach_country(ticker, [{'ticker': t} for t in holding_tickers]):
            trade_plan[ticker] = {'action': 'WATCH', 'shares_delta': 0, 'reasoning': 'Country limit breach', 'confidence': item['confidence']}
            _update_watchlist_action(ticker, 'add', item['confidence'])
            continue
        trade_plan[ticker] = {'action': 'STRONG_BUY', 'shares_delta': 1, 'reasoning': item['decision'].get('reasoning', ''), 'confidence': item['confidence']}
        holding_tickers.add(ticker)
        n_holdings += 1
        portfolio_full = n_holdings >= MAX_HOLDINGS

    # Process regular buys
    for item in sorted(buys, key=lambda x: -x['confidence']):
        ticker = item['ticker']
        if ticker in holding_tickers or ticker in trade_plan:
            continue
        if portfolio_full:
            trade_plan[ticker] = {'action': 'WATCH', 'shares_delta': 0, 'reasoning': 'Portfolio full', 'confidence': item['confidence']}
            _update_watchlist_action(ticker, 'add', item['confidence'])
            continue
        if _would_breach_sector(ticker, [{'ticker': t} for t in holding_tickers]):
            trade_plan[ticker] = {'action': 'WATCH', 'shares_delta': 0, 'reasoning': 'Sector limit', 'confidence': item['confidence']}
            _update_watchlist_action(ticker, 'add', item['confidence'])
            continue
        if _would_breach_country(ticker, [{'ticker': t} for t in holding_tickers]):
            trade_plan[ticker] = {'action': 'WATCH', 'shares_delta': 0, 'reasoning': 'Region limit', 'confidence': item['confidence']}
            _update_watchlist_action(ticker, 'add', item['confidence'])
            continue
        trade_plan[ticker] = {'action': 'BUY', 'shares_delta': 1, 'reasoning': item['decision'].get('reasoning', ''), 'confidence': item['confidence']}
        holding_tickers.add(ticker)
        n_holdings += 1
        portfolio_full = n_holdings >= MAX_HOLDINGS

    # Apply watchlist exit rules
    _apply_watchlist_exit_rules(watchlist_tickers, decisions)

    log('reconciliation', 'info', f'Trade plan: {len(trade_plan)} actions')
    for t, plan in trade_plan.items():
        if plan['action'] not in ('HOLD', 'AVOID', 'WATCH'):
            log('reconciliation', 'info', f"  {t}: {plan['action']} (conf={plan['confidence']:.2f})")

    return trade_plan


def _find_weakest_holding(holdings: list, decisions: dict) -> str:
    """Find holding with lowest conviction for rotation."""
    weakest = None
    lowest = 1.0
    for h in holdings:
        t = h['ticker']
        d = decisions.get(t, {})
        c = d.get('confidence', 0.5)
        if c < lowest:
            lowest = c
            weakest = t
    return weakest


if __name__ == '__main__':
    import json as _j
    from datetime import date
    from pathlib import Path as _P
    # Read today's TradingAgents recommendations from DB
    rows = query("""
        SELECT DISTINCT ON (ticker) ticker, action, confidence, reasoning,
               bull_case, bear_case, key_risks
        FROM recommendations
        WHERE date = %s AND signal_sources = 'trading_agents'
        ORDER BY ticker, confidence DESC
    """, (date.today(),))
    if not rows:
        log('reconciliation', 'info', 'No TradingAgents recommendations for today — nothing to reconcile')
    else:
        decisions = {r['ticker']: dict(r) for r in rows}
        contexts  = {t: {'conviction': d['confidence']} for t, d in decisions.items()}
        plan = reconcile(decisions, contexts)
        # Save trade_plan for optimizer (Decimal → float for JSON)
        _P('/tmp/advisor_trade_plan.json').write_text(
            _j.dumps(plan, default=lambda o: float(o) if hasattr(o, '__float__') else str(o))
        )
        log('reconciliation', 'info', f'Trade plan saved: {len(plan)} entries')
        print('\nTrade Plan:')
        for t, p in plan.items():
            if p['action'] not in ('HOLD', 'WATCH', 'AVOID'):
                print(f"  {t}: {p['action']} (conf={p['confidence']:.2f})")
