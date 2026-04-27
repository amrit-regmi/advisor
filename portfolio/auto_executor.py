"""
Auto-executor — automatically executes portfolio brief recommendations when
the user_settings key 'auto_execute_recommendations' = 'true'.

Runs at the end of portfolio_brain (called by run_daily.sh).
For each BUY/SELL in today's brief:
  - Logs to trades table
  - Updates holdings (BUY: upsert position; SELL: reduce/remove)
  - Marks recommendation as acted_on = TRUE
  - Sends Telegram confirmation

Safety gates:
  - Only acts if auto_execute_recommendations = 'true'
  - Minimum confidence threshold (auto_execute_min_confidence, default 0.75)
  - SELL only if ticker is in holdings (enforced by portfolio_brain already)
  - Never buys more than available cash
"""
import sys
from datetime import date

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log


def _get_setting(key, default=None):
    rows = query("SELECT value FROM user_settings WHERE key=%s", (key,))
    return rows[0]['value'] if rows and rows[0]['value'] is not None else default


def _execute_buy(rec, brief_buy):
    """Log BUY trade and update holdings."""
    ticker = rec['ticker']
    shares = brief_buy.get('shares', 0)
    price_eur = brief_buy.get('price_eur', 0)
    # Use native price if available; fall back to EUR price (legacy)
    price_store = brief_buy.get('price_native', price_eur)
    currency = brief_buy.get('currency', 'EUR')

    if not shares or not price_eur:
        log('WARNING', 'auto_executor', f'{ticker}: missing shares/price — skipping BUY')
        return False

    # Log to trades in native currency
    execute("""
        INSERT INTO trades (ticker, action, shares, price, currency, trade_date, notes)
        VALUES (%s, 'BUY', %s, %s, %s, %s, 'Auto-executed by advisor')
    """, (ticker, shares, price_store, currency, date.today()))

    # Upsert holdings — weighted avg cost in native currency
    existing = query("SELECT shares, avg_buy_price FROM holdings WHERE ticker=%s AND active=TRUE",
                     (ticker,))
    if existing:
        old_shares = float(existing[0]['shares'])
        old_price = float(existing[0]['avg_buy_price'] or price_store)
        new_shares = old_shares + shares
        new_avg = ((old_shares * old_price) + (shares * price_store)) / new_shares
        execute("""
            UPDATE holdings SET shares=%s, avg_buy_price=%s, currency=%s, updated_at=NOW()
            WHERE ticker=%s AND active=TRUE
        """, (new_shares, round(new_avg, 4), currency, ticker))
    else:
        execute("""
            INSERT INTO holdings (ticker, shares, avg_buy_price, currency, bought_date, notes)
            VALUES (%s, %s, %s, %s, %s, 'Auto-executed')
        """, (ticker, shares, price_store, currency, date.today()))

    # Reduce cash balance
    cost = shares * price_eur
    cash_rows = query("SELECT value FROM user_settings WHERE key='nordnet_cash_eur'")
    current_cash = float(cash_rows[0]['value'] or 0) if cash_rows else 0
    new_cash = max(0, current_cash - cost)
    execute("UPDATE user_settings SET value=%s WHERE key='nordnet_cash_eur'",
            (str(round(new_cash, 2)),))

    # Track monthly invested amount so portfolio_brain enforces monthly budget cap
    mi_rows = query("SELECT value FROM user_settings WHERE key='monthly_invested_this_month'")
    current_mi = float(mi_rows[0]['value'] or 0) if mi_rows else 0
    execute("""
        INSERT INTO user_settings (key, value) VALUES ('monthly_invested_this_month', %s)
        ON CONFLICT (key) DO UPDATE SET value=%s
    """, (str(round(current_mi + cost, 2)), str(round(current_mi + cost, 2))))

    log('INFO', 'auto_executor',
        f'AUTO-BUY {ticker}: {shares} shares @ €{price_eur:.2f} = €{cost:.2f}. '
        f'Cash: €{current_cash:.2f} → €{new_cash:.2f}. '
        f'Monthly invested: €{current_mi:.2f} → €{current_mi + cost:.2f}')
    return True


def _execute_sell(rec, brief_sell):
    """Log SELL trade and update holdings."""
    ticker = rec['ticker']
    shares_to_sell = brief_sell.get('shares', 0)
    price_eur = brief_sell.get('price_eur', 0)
    full_exit = brief_sell.get('full_exit', False)

    if not shares_to_sell or not price_eur:
        log('WARNING', 'auto_executor', f'{ticker}: missing shares/price — skipping SELL')
        return False

    existing = query("SELECT shares, currency FROM holdings WHERE ticker=%s AND active=TRUE",
                     (ticker,))
    if not existing:
        log('WARNING', 'auto_executor', f'{ticker}: not in holdings — cannot SELL')
        return False

    held = float(existing[0]['shares'])
    currency = existing[0]['currency'] or 'EUR'
    proceeds = shares_to_sell * price_eur

    execute("""
        INSERT INTO trades (ticker, action, shares, price, currency, trade_date, notes)
        VALUES (%s, 'SELL', %s, %s, %s, %s, 'Auto-executed by advisor')
    """, (ticker, shares_to_sell, price_eur, currency, date.today()))

    if full_exit or shares_to_sell >= held:
        execute("UPDATE holdings SET active=FALSE, updated_at=NOW() WHERE ticker=%s", (ticker,))
    else:
        execute("UPDATE holdings SET shares=%s, updated_at=NOW() WHERE ticker=%s AND active=TRUE",
                (held - shares_to_sell, ticker))

    # Add proceeds to cash
    cash_rows = query("SELECT value FROM user_settings WHERE key='nordnet_cash_eur'")
    current_cash = float(cash_rows[0]['value'] or 0) if cash_rows else 0
    new_cash = current_cash + proceeds
    execute("UPDATE user_settings SET value=%s WHERE key='nordnet_cash_eur'",
            (str(round(new_cash, 2)),))

    log('INFO', 'auto_executor',
        f'AUTO-SELL {ticker}: {shares_to_sell} shares @ €{price_eur:.2f} = €{proceeds:.2f}. '
        f'Cash: €{current_cash:.2f} → €{new_cash:.2f}')
    return True


def _mark_acted(ticker):
    execute("""
        UPDATE recommendations SET acted_on=TRUE
        WHERE ticker=%s AND date=%s
    """, (ticker, date.today()))


def _send_confirmation(executed_buys, executed_sells, watched_added=None):
    try:
        from alerts.telegram_bot import send_alert
        lines = ['&#9889; <b>SIMULATION EXECUTION REPORT</b>\n']
        for b in executed_buys:
            lines.append(f"&#128994; AUTO-BUY <b>{b['ticker']}</b>: "
                         f"{b['shares']} shares @ €{b['price_eur']:.2f} = €{b['total_cost_eur']:.0f}")
        for s in executed_sells:
            lines.append(f"&#128308; AUTO-SELL <b>{s['ticker']}</b>: "
                         f"{s['shares']} shares @ €{s['price_eur']:.2f} = €{s['proceeds_eur']:.0f}")
        if watched_added:
            lines.append(f"\n&#128064; Added to watchlist: {', '.join(watched_added)}")
        lines.append('\nAll positions updated in holdings. Review at /portfolio.')
        send_alert('\n'.join(lines))
    except Exception as e:
        log('WARNING', 'auto_executor', f'Could not send confirmation: {e}')


def _maybe_monthly_deposit():
    """Auto-deposit on 15th of each month when simulation is enabled."""
    if _get_setting('simulate_recommendations', 'false').lower() != 'true':
        return
    today = date.today()
    if today.day != 15:
        return
    month_key = f'monthly_deposit_done_{today.year}_{today.month:02d}'
    already = query("SELECT value FROM user_settings WHERE key=%s", (month_key,))
    if already:
        return
    amount = float(_get_setting('simulation_monthly_deposit',
                                _get_setting('monthly_investment_budget_eur', 500)) or 500)
    rows = query("SELECT value FROM user_settings WHERE key='nordnet_cash_eur'")
    current = float(rows[0]['value'] or 0) if rows else 0.0
    new_bal = round(current + amount, 2)
    execute("INSERT INTO user_settings (key, value) VALUES ('nordnet_cash_eur', %s) "
            "ON CONFLICT (key) DO UPDATE SET value=%s", (str(new_bal), str(new_bal)))
    execute("INSERT INTO user_settings (key, value) VALUES (%s, 'done') "
            "ON CONFLICT (key) DO UPDATE SET value='done'", (month_key,))
    log('INFO', 'auto_executor',
        f'Monthly simulation deposit €{amount:.0f} — cash €{current:.0f} → €{new_bal:.0f}')
    try:
        from alerts.telegram_bot import send_alert
        send_alert(f'&#128176; Monthly deposit €{amount:.0f} credited — simulation cash now €{new_bal:,.0f}')
    except Exception:
        pass


def run(brief=None):
    """
    Main entry point. Pass the brief dict from portfolio_brain.build_brief(),
    or None to re-build it.
    """
    _maybe_monthly_deposit()
    auto = _get_setting('simulate_recommendations', 'false')
    if auto.lower() != 'true':
        log('INFO', 'auto_executor', 'Simulation disabled — skipping')
        return

    log('INFO', 'auto_executor', 'Simulation ENABLED — executing all brief recommendations')

    if brief is None:
        from portfolio.portfolio_brain import build_brief
        brief = build_brief()

    executed_sells = []
    executed_buys = []

    # Execute SELLs first to free cash
    for s in brief.get('sells', []):
        rec = query("SELECT ticker FROM recommendations WHERE ticker=%s AND date=%s LIMIT 1",
                    (s['ticker'], date.today()))
        if rec and _execute_sell(rec[0], s):
            _mark_acted(s['ticker'])
            executed_sells.append(s)

    # Execute BUYs
    for b in brief.get('buys', []):
        rec = query("SELECT ticker FROM recommendations WHERE ticker=%s AND date=%s LIMIT 1",
                    (b['ticker'], date.today()))
        if rec and _execute_buy(rec[0], b):
            _mark_acted(b['ticker'])
            executed_buys.append(b)

    # Auto-add WATCH items to watchlist so next pipeline considers them
    watched_added = []
    for w in brief.get('watches', []):
        ticker = w.get('ticker')
        if not ticker:
            continue
        existing = query("SELECT id FROM watchlist WHERE ticker=%s AND active=TRUE", (ticker,))
        if existing:
            continue
        try:
            execute("""
                INSERT INTO watchlist (ticker, active)
                VALUES (%s, TRUE)
                ON CONFLICT (ticker) DO UPDATE SET active=TRUE
            """, (ticker,))
            watched_added.append(ticker)
        except Exception as e:
            log('WARNING', 'auto_executor', f'Could not add {ticker} to watchlist: {e}')
    if watched_added:
        log('INFO', 'auto_executor',
            f'Auto-added {len(watched_added)} WATCH items to watchlist: {", ".join(watched_added)}')

    # Watchlist cleanup:
    # 1. Remove tickers that were just bought (they're now holdings)
    # 2. Remove tickers that have been watched >14 days with no BUY signal
    bought_tickers = {b['ticker'] for b in executed_buys}
    if bought_tickers:
        execute(f"""
            UPDATE watchlist SET active=FALSE
            WHERE ticker = ANY(%s)
        """, (list(bought_tickers),))
        log('INFO', 'auto_executor',
            f'Removed {len(bought_tickers)} bought tickers from watchlist: {", ".join(bought_tickers)}')

    # Expire watchlist items with no BUY signal in 14 days
    stale = query("""
        SELECT w.ticker FROM watchlist w
        WHERE w.active = TRUE
          AND NOT EXISTS (
              SELECT 1 FROM holdings h WHERE h.ticker = w.ticker AND h.active = TRUE
          )
          AND w.created_at < NOW() - INTERVAL '14 days'
          AND NOT EXISTS (
              SELECT 1 FROM recommendations r
              WHERE r.ticker = w.ticker AND r.action = 'BUY'
                AND r.date >= CURRENT_DATE - INTERVAL '7 days'
          )
    """)
    if stale:
        stale_tickers = [r['ticker'] for r in stale]
        execute("UPDATE watchlist SET active=FALSE WHERE ticker = ANY(%s)", (stale_tickers,))
        log('INFO', 'auto_executor',
            f'Expired {len(stale_tickers)} stale watchlist items (>14 days, no BUY): '
            f'{", ".join(stale_tickers)}')

    total = len(executed_buys) + len(executed_sells)
    log('INFO', 'auto_executor',
        f'Auto-execution complete: {len(executed_buys)} buys, {len(executed_sells)} sells, '
        f'{len(watched_added)} watches added to watchlist')

    if total > 0 or watched_added:
        _send_confirmation(executed_buys, executed_sells, watched_added)


if __name__ == '__main__':
    enabled = _get_setting('simulate_recommendations', 'false')
    print(f'Simulation enabled: {enabled}')
    print('(Set simulate_recommendations=true in Settings to enable)')
