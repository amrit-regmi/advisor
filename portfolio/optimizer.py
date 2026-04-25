"""
Portfolio Optimizer — converts trade_plan qualitative actions into numeric target weights.
Uses PyPortfolioOpt: maximize risk-adjusted return, minimize volatility and turnover.
Constraints: sum=100%, cash>=5%, max_position=12%, sector caps from user targets.
"""
import sys
import json
from datetime import date, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, log
from portfolio.sector_utils import normalize as normalize_sector, country_to_region, DEFAULT_REGION_TARGETS

try:
    from pypfopt import EfficientFrontier, risk_models, expected_returns
    from pypfopt.discrete_allocation import DiscreteAllocation, get_latest_prices
    _PYPFOPT_OK = True
except ImportError:
    _PYPFOPT_OK = False

# Action → target weight range [min%, max%]
ACTION_WEIGHT_RANGE = {
    'STRONG_BUY': (0.06, 0.12),
    'BUY': (0.03, 0.08),
    'HOLD': (None, None),   # maintain current
    'SELL': (0.0, 0.0),
    'AVOID': (0.0, 0.0),
    'WATCH': (0.0, 0.0),
}

MAX_POSITION = 0.12
CASH_MIN = 0.05
MAX_SECTOR = 0.25  # fallback if no sector target configured


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


def _ticker_sectors(tickers: list) -> dict:
    """Return {ticker: canonical_sector} for a list of tickers."""
    if not tickers:
        return {}
    rows = query("SELECT ticker, sector FROM universe WHERE ticker = ANY(%s)", (tickers,))
    return {r['ticker']: normalize_sector(r['sector'] or '') for r in rows}


def _ticker_regions(tickers: list) -> dict:
    """Return {ticker: canonical_region} for a list of tickers."""
    if not tickers:
        return {}
    rows = query("SELECT ticker, country FROM universe WHERE ticker = ANY(%s)", (tickers,))
    return {r['ticker']: country_to_region(r['country'] or 'US') for r in rows}


def _build_sector_constraints(ef, tickers: list, sector_targets: dict) -> None:
    """Add per-sector weight cap constraints to an EfficientFrontier."""
    ticker_sector = _ticker_sectors(tickers)
    sector_indices: dict[str, list[int]] = {}
    for i, t in enumerate(tickers):
        sec = ticker_sector.get(t, 'Unknown')
        sector_indices.setdefault(sec, []).append(i)

    for sec, indices in sector_indices.items():
        if sec == 'Unknown' or not indices:
            continue
        tgt_pct = float(sector_targets.get(sec, MAX_SECTOR * 100))
        cap = min((tgt_pct + 5) / 100, 0.40)
        cap = max(cap, 0.05)
        idxs = indices
        ef.add_constraint(lambda w, idxs=idxs, cap=cap: sum(w[i] for i in idxs) <= cap)


def _build_region_constraints(ef, tickers: list, region_targets: dict) -> None:
    """Add per-region weight cap constraints to an EfficientFrontier."""
    ticker_region = _ticker_regions(tickers)
    region_indices: dict[str, list[int]] = {}
    for i, t in enumerate(tickers):
        reg = ticker_region.get(t, 'Other')
        region_indices.setdefault(reg, []).append(i)

    for reg, indices in region_indices.items():
        if not indices:
            continue
        tgt_pct = float(region_targets.get(reg, 50))  # 50% default for unknown regions
        cap = min((tgt_pct + 10) / 100, 0.80)  # 10% tolerance
        cap = max(cap, 0.10)
        idxs = indices
        ef.add_constraint(lambda w, idxs=idxs, cap=cap: sum(w[i] for i in idxs) <= cap)


def _load_price_matrix(tickers: list, days: int = 90) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    cutoff = date.today() - timedelta(days=days)
    rows = query("""
        SELECT ticker, date, close FROM prices
        WHERE ticker = ANY(%s) AND date >= %s
        ORDER BY date ASC
    """, (tickers, cutoff))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    pivot = df.pivot(index='date', columns='ticker', values='close')
    pivot = pivot.dropna(how='all').ffill()
    return pivot


def _equal_weights(tickers: list) -> dict:
    if not tickers:
        return {}
    w = 1.0 / len(tickers)
    return {t: w for t in tickers}


def _get_current_weights(holdings: list, total_value: float) -> dict:
    """Compute current portfolio weights from holdings."""
    if not holdings or total_value <= 0:
        return {}
    weights = {}
    for h in holdings:
        row = query("SELECT close FROM prices WHERE ticker = %s ORDER BY date DESC LIMIT 1", (h['ticker'],))
        if row:
            value = float(row[0]['close']) * float(h.get('shares', 0))
            weights[h['ticker']] = value / total_value
    return weights


def compute_target_weights(trade_plan: dict, cash_eur: float) -> dict:
    """
    Given trade_plan from reconciliation layer, compute target weights.
    Returns {ticker: target_weight} with 'CASH' key for uninvested portion.
    """
    action_tickers = {t: p['action'] for t, p in trade_plan.items()}
    buy_tickers = [t for t, a in action_tickers.items() if a in ('BUY', 'STRONG_BUY')]
    hold_tickers = [t for t, a in action_tickers.items() if a == 'HOLD']
    sell_tickers = [t for t, a in action_tickers.items() if a in ('SELL', 'AVOID')]

    # Tickers that need optimized weights
    investable = buy_tickers + hold_tickers
    if not investable:
        log('optimizer', 'info', 'No investable tickers — returning all cash')
        return {'CASH': 1.0}

    prices = _load_price_matrix(investable)
    target_weights = {}

    sector_targets = _load_sector_targets()
    region_targets = _load_region_targets()
    if _PYPFOPT_OK and len(prices.columns) >= 2 and len(prices) >= 20:
        try:
            mu = expected_returns.mean_historical_return(prices)
            S = risk_models.sample_cov(prices)
            ef = EfficientFrontier(mu, S)

            # Per-position cap
            cols = list(prices.columns)
            for t in investable:
                if t in cols:
                    ef.add_constraint(lambda w, i=cols.index(t): w[i] <= MAX_POSITION)

            # Sector and region caps from user targets
            if sector_targets:
                _build_sector_constraints(ef, cols, sector_targets)
            if region_targets:
                _build_region_constraints(ef, cols, region_targets)

            ef.max_sharpe(risk_free_rate=0.03)
            cleaned = ef.clean_weights()
            target_weights = dict(cleaned)
            log('optimizer', 'info', f'PyPortfolioOpt weights computed for {len(target_weights)} tickers')
        except Exception as e:
            log('optimizer', 'warn', f'PyPortfolioOpt failed: {e} — using equal weights')
            target_weights = _equal_weights(investable)
    else:
        target_weights = _equal_weights(investable)

    # Enforce sell = 0
    for t in sell_tickers:
        target_weights[t] = 0.0

    # Scale to leave cash_min
    total_invested = sum(target_weights.values())
    if total_invested > (1 - CASH_MIN):
        scale = (1 - CASH_MIN) / total_invested
        target_weights = {t: w * scale for t, w in target_weights.items()}

    # Clip to position limits
    target_weights = {t: min(w, MAX_POSITION) for t, w in target_weights.items()}

    # Add cash
    cash_weight = 1.0 - sum(target_weights.values())
    target_weights['CASH'] = max(CASH_MIN, cash_weight)

    log('optimizer', 'info', f'Target weights: {target_weights}')
    return target_weights


def weights_to_shares(target_weights: dict, portfolio_value_eur: float, cash_eur: float) -> dict:
    """
    Convert target weights to share quantities.
    Returns {ticker: shares_to_buy_or_sell} — positive = buy, negative = sell.
    """
    current_holdings = query("SELECT ticker, shares FROM holdings WHERE active = true AND shares > 0")
    current_qty = {r['ticker']: float(r['shares']) for r in current_holdings}

    orders = {}
    for ticker, weight in target_weights.items():
        if ticker == 'CASH':
            continue
        target_value = weight * portfolio_value_eur
        price_row = query("SELECT close FROM prices WHERE ticker = %s ORDER BY date DESC LIMIT 1", (ticker,))
        if not price_row or not price_row[0]['close']:
            continue
        price = float(price_row[0]['close'])
        target_shares = int(target_value / price)
        current_shares = int(current_qty.get(ticker, 0))
        delta = target_shares - current_shares
        if delta != 0:
            orders[ticker] = delta

    return orders


def optimize_candidates(tickers: list) -> dict:
    """
    Compute optimal weights for a list of BUY candidate tickers using PyPortfolioOpt.
    Used by portfolio_brain to get sizing hints before conviction gating.
    Returns {ticker: weight} or {} on failure (caller falls back to equal-weight).
    """
    if not tickers:
        return {}

    prices = _load_price_matrix(tickers)
    cols = list(prices.columns) if not prices.empty else tickers

    sector_targets = _load_sector_targets()
    region_targets = _load_region_targets()
    if _PYPFOPT_OK and len(prices.columns) >= 2 and len(prices) >= 20:
        try:
            mu = expected_returns.mean_historical_return(prices)
            S = risk_models.sample_cov(prices)
            ef = EfficientFrontier(mu, S)
            for i in range(len(cols)):
                ef.add_constraint(lambda w, i=i: w[i] <= MAX_POSITION)
            if sector_targets:
                _build_sector_constraints(ef, cols, sector_targets)
            if region_targets:
                _build_region_constraints(ef, cols, region_targets)
            ef.max_sharpe(risk_free_rate=0.03)
            cleaned = ef.clean_weights()
            result = {t: float(w) for t, w in cleaned.items() if w > 0.001}
            log('optimizer', 'info',
                f'optimize_candidates: {len(result)} weights for {len(tickers)} candidates')
            return result
        except Exception as e:
            log('optimizer', 'warn', f'optimize_candidates PyPortfolioOpt failed: {e}')

    return _equal_weights(tickers)


def get_weight_hint(ticker: str, opt_weights: dict):
    """Return the optimizer weight for a ticker, or None if not present."""
    return opt_weights.get(ticker) if opt_weights else None


if __name__ == '__main__':
    import json as _j
    from pathlib import Path as _P
    _plan_file = _P('/tmp/advisor_trade_plan.json')
    if _plan_file.exists():
        trade_plan = _j.loads(_plan_file.read_text())
        log('optimizer', 'info', f'Loaded trade_plan: {len(trade_plan)} entries')
    else:
        log('optimizer', 'info', 'No trade_plan file — optimizer skipped (portfolio_brain handles sizing)')
        raise SystemExit(0)
    cash_rows = query("SELECT value FROM user_settings WHERE key='nordnet_cash_eur'")
    cash = float(cash_rows[0]['value'] or 2000) if cash_rows else 2000
    weights = compute_target_weights(trade_plan, cash_eur=cash)
    log('optimizer', 'info', f'Target weights: {weights}')
    print('Target weights:')
    for t, w in sorted(weights.items(), key=lambda x: -x[1]):
        print(f'  {t}: {w:.1%}')
