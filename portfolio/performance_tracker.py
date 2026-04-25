"""
Daily portfolio performance snapshot.
Writes one row to performance_history per day.
Also computes accuracy metrics into strategy_metrics by comparing
historical recommendations against subsequent price outcomes.
Runs at end of daily pipeline (after portfolio_brain).
"""
import sys
import json
import numpy as np
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log


def _get_live_fx():
    """Load EUR/currency rates from macro_data (same logic as portfolio_brain)."""
    rows = query("""
        SELECT series_id, value FROM macro_data
        WHERE series_id IN ('DEXUSEU','DEXUSUK','DEXJPUS','DEXUSAL',
                            'DEXSDUS','DEXSZUS','DEXHKUS','DEXDNUS')
        AND date >= CURRENT_DATE - INTERVAL '30 days'
        ORDER BY series_id, date DESC
    """)
    seen = {}
    for r in rows:
        if r['series_id'] not in seen and r['value'] is not None:
            seen[r['series_id']] = float(r['value'])

    if not seen:
        return {'EUR': 1.0, 'USD': 0.853, 'GBP': 1.17, 'JPY': 0.0063,
                'AUD': 0.59, 'SEK': 0.078, 'CHF': 1.06, 'HKD': 0.11}

    usd_per_eur = seen.get('DEXUSEU', 1.17)
    eur_per_usd = 1.0 / usd_per_eur
    return {
        'EUR': 1.0,
        'USD': eur_per_usd,
        'GBP': seen['DEXUSUK'] / usd_per_eur if 'DEXUSUK' in seen else 1.17,
        'JPY': eur_per_usd / seen['DEXJPUS'] if 'DEXJPUS' in seen else 0.0063,
        'AUD': eur_per_usd / seen['DEXUSAL'] if 'DEXUSAL' in seen else 0.59,
        'SEK': eur_per_usd / seen['DEXSDUS'] if 'DEXSDUS' in seen else 0.078,
        'CHF': eur_per_usd / seen['DEXSZUS'] if 'DEXSZUS' in seen else 1.06,
        'HKD': eur_per_usd / seen['DEXHKUS'] if 'DEXHKUS' in seen else 0.11,
    }


def snapshot_portfolio():
    """
    Record today's portfolio value into performance_history.
    Returns the snapshot dict.
    """
    fx = _get_live_fx()

    cash_row = query("SELECT value FROM user_settings WHERE key='nordnet_cash_eur'")
    cash = float(cash_row[0]['value']) if cash_row and cash_row[0]['value'] else 0.0

    holdings = query("""
        SELECT h.ticker, h.shares, h.avg_buy_price, h.currency
        FROM holdings h WHERE h.active = TRUE
    """)

    positions = {}
    total_value = 0.0

    for h in holdings:
        ticker = h['ticker']
        pr = query("SELECT close FROM prices WHERE ticker=%s ORDER BY date DESC LIMIT 1",
                   (ticker,))
        if not pr:
            continue
        price = float(pr[0]['close'])
        shares = float(h['shares'])
        currency = h['currency'] or 'USD'
        rate = fx.get(currency, fx['USD'])
        value_eur = shares * price * rate
        total_value += value_eur
        positions[ticker] = {
            'shares': shares,
            'price': price,
            'currency': currency,
            'value_eur': round(value_eur, 2),
        }

    execute("""
        INSERT INTO performance_history (date, portfolio_value, cash, positions)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (date) DO UPDATE SET
            portfolio_value = EXCLUDED.portfolio_value,
            cash            = EXCLUDED.cash,
            positions       = EXCLUDED.positions
    """, (date.today(), round(total_value, 2), round(cash, 2),
          json.dumps(positions)))

    log('INFO', 'performance_tracker',
        f'Snapshot: portfolio=€{total_value:.2f}, cash=€{cash:.2f}, '
        f'{len(positions)} positions')

    return {
        'date': date.today(),
        'portfolio_value': total_value,
        'cash': cash,
        'positions': positions,
    }


def compute_accuracy_metrics():
    """
    Assess recommendation accuracy:
    - BUY: outperformed if price N days later > price at recommendation
    - SELL: avoided loss if price N days later < price at recommendation
    Writes results to strategy_metrics.
    """
    HORIZON_DAYS = 30  # evaluation window

    cutoff = date.today() - timedelta(days=HORIZON_DAYS + 5)

    recs = query("""
        SELECT ticker, action, date, confidence
        FROM recommendations
        WHERE date >= %s AND date <= %s AND action IN ('BUY','SELL')
        ORDER BY date DESC
    """, (cutoff, date.today() - timedelta(days=HORIZON_DAYS)))

    if not recs:
        log('INFO', 'performance_tracker', 'No mature recommendations to evaluate')
        return

    profitable_buys = 0
    loss_avoided_sells = 0
    total_buy = 0
    total_sell = 0
    returns = []

    for r in recs:
        ticker = r['ticker']
        rec_date = r['date']
        action = r['action']
        eval_date = rec_date + timedelta(days=HORIZON_DAYS)

        entry = query("""
            SELECT close FROM prices
            WHERE ticker=%s AND date >= %s
            ORDER BY date ASC LIMIT 1
        """, (ticker, rec_date))
        exit_ = query("""
            SELECT close FROM prices
            WHERE ticker=%s AND date >= %s
            ORDER BY date ASC LIMIT 1
        """, (ticker, eval_date))

        if not entry or not exit_:
            continue

        p0 = float(entry[0]['close'])
        p1 = float(exit_[0]['close'])
        if p0 == 0:
            continue

        ret = (p1 - p0) / p0
        returns.append(ret)

        if action == 'BUY':
            total_buy += 1
            if ret > 0:
                profitable_buys += 1
        elif action == 'SELL':
            total_sell += 1
            if ret < 0:
                loss_avoided_sells += 1

    if not returns:
        return

    total_recs = len(returns)
    hit_rate = (profitable_buys + loss_avoided_sells) / total_recs
    avg_return = float(np.mean(returns))
    cumulative_pnl = round(avg_return * 10000, 2)  # on €10k base

    # Simple Sharpe from returns
    if len(returns) > 1 and np.std(returns) > 0:
        sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(252 / HORIZON_DAYS))
    else:
        sharpe = 0.0

    max_dd = float(min(returns)) if returns else 0.0

    execute("""
        INSERT INTO strategy_metrics
            (date, sharpe, max_drawdown, hit_rate, cumulative_pnl,
             total_recommendations, profitable_buys, loss_avoided_sells)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (date) DO UPDATE SET
            sharpe                = EXCLUDED.sharpe,
            max_drawdown          = EXCLUDED.max_drawdown,
            hit_rate              = EXCLUDED.hit_rate,
            cumulative_pnl        = EXCLUDED.cumulative_pnl,
            total_recommendations = EXCLUDED.total_recommendations,
            profitable_buys       = EXCLUDED.profitable_buys,
            loss_avoided_sells    = EXCLUDED.loss_avoided_sells
    """, (date.today(), round(sharpe, 4), round(max_dd, 4),
          round(hit_rate, 4), cumulative_pnl,
          total_recs, profitable_buys, loss_avoided_sells))

    log('INFO', 'performance_tracker',
        f'Accuracy: hit_rate={hit_rate:.1%}, buys_won={profitable_buys}/{total_buy}, '
        f'sells_avoided={loss_avoided_sells}/{total_sell}, '
        f'cumPnL=€{cumulative_pnl:.0f}')


def run():
    log('INFO', 'performance_tracker', 'Starting daily performance snapshot...')
    snapshot_portfolio()
    compute_accuracy_metrics()
    log('INFO', 'performance_tracker', 'Performance tracking complete')


if __name__ == '__main__':
    run()
    snap = query("""
        SELECT date, portfolio_value, cash FROM performance_history
        ORDER BY date DESC LIMIT 5
    """)
    print('\n--- Performance History (last 5 days) ---')
    for r in snap:
        print(f"  {r['date']}: €{float(r['portfolio_value']):.2f} "
              f"(cash €{float(r['cash']):.2f})")

    metrics = query("""
        SELECT date, hit_rate, cumulative_pnl, total_recommendations
        FROM strategy_metrics ORDER BY date DESC LIMIT 3
    """)
    print('\n--- Strategy Metrics ---')
    for m in metrics:
        hr = float(m['hit_rate'] or 0)
        pnl = float(m['cumulative_pnl'] or 0)
        print(f"  {m['date']}: hit={hr:.1%}, cumPnL=€{pnl:.0f}, "
              f"recs={m['total_recommendations']}")
