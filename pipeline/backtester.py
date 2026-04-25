"""
Walk-forward backtester using our recommendation history and prices table.
Uses zero new API calls — purely DB data.
Runs weekly (cron: 0 5 * * 0).
"""
import sys
import backtrader as bt
import pandas as pd
import numpy as np
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import execute, query, log


def _load_prices(ticker, start_date, end_date):
    rows = query("""
        SELECT date, open, high, low, close, volume
        FROM prices
        WHERE ticker=%s AND date BETWEEN %s AND %s
        ORDER BY date ASC
    """, (ticker, start_date, end_date))
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date')
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    # backtrader needs openinterest column
    df['openinterest'] = 0
    return df


def _load_signals(ticker, start_date, end_date):
    """Return {date: action} from recommendations table."""
    rows = query("""
        SELECT date, action FROM recommendations
        WHERE ticker=%s AND date BETWEEN %s AND %s
        ORDER BY date ASC
    """, (ticker, start_date, end_date))
    return {r['date']: r['action'] for r in rows}


class SignalStrategy(bt.Strategy):
    params = (('signals', {}),)

    def __init__(self):
        self.signals = self.params.signals
        self.trade_log = []

    def next(self):
        dt = self.data.datetime.date(0)
        action = self.signals.get(dt)
        if action == 'BUY' and not self.position:
            self.buy()
        elif action == 'SELL' and self.position:
            self.sell()

    def notify_trade(self, trade):
        if trade.isclosed:
            self.trade_log.append({
                'pnl': trade.pnl,
                'pnlcomm': trade.pnlcomm,
            })


def _compute_metrics(cerebro, strategy, initial_cash):
    """Extract win_rate, sharpe, drawdown, total_return from cerebro run."""
    final_value = cerebro.broker.getvalue()
    total_return = (final_value - initial_cash) / initial_cash

    trades = strategy.trade_log
    if not trades:
        return {
            'win_rate': 0.0,
            'sharpe_ratio': 0.0,
            'max_drawdown': 0.0,
            'total_return': round(total_return, 6),
        }

    wins = sum(1 for t in trades if t['pnl'] > 0)
    win_rate = wins / len(trades) if trades else 0.0

    pnls = [t['pnl'] for t in trades]
    if len(pnls) > 1 and np.std(pnls) > 0:
        sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252 / max(len(pnls), 1))
    else:
        sharpe = 0.0

    # Max drawdown from equity curve via analyzer
    dd_analyzer = cerebro.runstrats[0][0].analyzers.drawdown.get_analysis()
    max_dd = dd_analyzer.get('max', {}).get('drawdown', 0.0) / 100.0

    return {
        'win_rate': round(win_rate, 4),
        'sharpe_ratio': round(sharpe, 4),
        'max_drawdown': round(max_dd, 4),
        'total_return': round(total_return, 6),
    }


def backtest_ticker(ticker, start_date=None, end_date=None, strategy='signal_follow'):
    """
    Run walk-forward backtest for one ticker.
    Returns metrics dict or None if insufficient data.
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)

    df = _load_prices(ticker, start_date, end_date)
    if df is None or len(df) < 20:
        log('WARNING', 'backtester', f'{ticker}: insufficient price data')
        return None

    signals = _load_signals(ticker, start_date, end_date)
    log('INFO', 'backtester',
        f'{ticker}: {len(df)} price days, {len(signals)} signals')

    data_feed = bt.feeds.PandasData(dataname=df)

    cerebro = bt.Cerebro()
    cerebro.adddata(data_feed)
    cerebro.addstrategy(SignalStrategy, signals=signals)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.broker.setcash(10000.0)
    cerebro.broker.setcommission(commission=0.001)  # 0.1% per trade

    initial_cash = cerebro.broker.getvalue()
    results = cerebro.run()
    strat = results[0]

    metrics = _compute_metrics(cerebro, strat, initial_cash)
    metrics['ticker'] = ticker
    metrics['strategy'] = strategy
    metrics['period_start'] = start_date
    metrics['period_end'] = end_date

    _save_results(metrics)
    log('INFO', 'backtester',
        f'{ticker}: win={metrics["win_rate"]:.1%}, sharpe={metrics["sharpe_ratio"]:.2f}, '
        f'dd={metrics["max_drawdown"]:.1%}, ret={metrics["total_return"]:.1%}')
    return metrics


def _save_results(m):
    execute("""
        INSERT INTO backtest_results
            (ticker, strategy, period_start, period_end,
             win_rate, sharpe_ratio, max_drawdown, total_return)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        m['ticker'], m['strategy'], m['period_start'], m['period_end'],
        m['win_rate'], m['sharpe_ratio'], m['max_drawdown'], m['total_return'],
    ))


def _save_strategy_metrics(results):
    """Aggregate per-ticker metrics into daily strategy_metrics row."""
    if not results:
        return
    avg_sharpe = sum(r['sharpe_ratio'] for r in results) / len(results)
    max_dd = max(r['max_drawdown'] for r in results)
    avg_win_rate = sum(r['win_rate'] for r in results) / len(results)
    total_return = sum(r['total_return'] for r in results)

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
    """, (
        date.today(),
        round(avg_sharpe, 4),
        round(max_dd, 4),
        round(avg_win_rate, 4),
        round(total_return * 10000, 2),  # scaled to EUR-equivalent on 10k base
        len(results),
        sum(1 for r in results if r['total_return'] > 0),
        sum(1 for r in results if r['total_return'] < 0),  # SELLs that avoided loss
    ))
    log('INFO', 'backtester',
        f'Strategy metrics: sharpe={avg_sharpe:.2f}, hit_rate={avg_win_rate:.1%}, '
        f'max_dd={max_dd:.1%}')


def run_all():
    """Weekly run: backtest all tickers that have recommendations."""
    log('INFO', 'backtester', 'Starting weekly backtest run...')
    tickers = query("""
        SELECT DISTINCT ticker FROM recommendations
        WHERE date >= %s
    """, (date.today() - timedelta(days=90),))

    if not tickers:
        log('INFO', 'backtester', 'No recommendation history — skipping')
        return []

    results = []
    for row in tickers:
        try:
            m = backtest_ticker(row['ticker'])
            if m:
                results.append(m)
        except Exception as e:
            log('ERROR', 'backtester', f"{row['ticker']}: {e}")

    _save_strategy_metrics(results)
    log('INFO', 'backtester', f'Backtest complete: {len(results)} tickers')
    return results


if __name__ == '__main__':
    print("Running backtest on AAPL using prices table data...")
    m = backtest_ticker('AAPL')
    if m:
        print(f"\nAAPL backtest results:")
        print(f"  Period:       {m['period_start']} → {m['period_end']}")
        print(f"  Win rate:     {m['win_rate']:.1%}")
        print(f"  Sharpe ratio: {m['sharpe_ratio']:.2f}")
        print(f"  Max drawdown: {m['max_drawdown']:.1%}")
        print(f"  Total return: {m['total_return']:.1%}")
    else:
        print("No data — run collect_all.py first to populate prices table")
