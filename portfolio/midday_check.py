"""
Mid-day reverification (runs at 12:00 on weekdays).
Checks unacted recommendations for sanity triggers.
Sends Telegram alert if any ticker is flagged — does NOT re-run TradingAgents globally.
Users can manually trigger selective re-analysis from dashboard.
"""
import sys
from datetime import date

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, log
from pipeline.sanity_check import run_sanity_checks


def run():
    log('INFO', 'midday_check', 'Starting mid-day reverification...')

    # Only check tickers with unacted recommendations from today
    unacted = query("""
        SELECT ticker, date, action, confidence FROM recommendations
        WHERE date = %s AND acted_on = FALSE
        ORDER BY confidence DESC
    """, (date.today(),))

    if not unacted:
        log('INFO', 'midday_check', 'No unacted recommendations — nothing to check')
        return {}

    log('INFO', 'midday_check',
        f'Checking {len(unacted)} unacted recommendations: '
        f'{", ".join(r["ticker"] for r in unacted if r["ticker"])}')

    flagged = run_sanity_checks([dict(r) for r in unacted])

    if flagged:
        _send_midday_alert(flagged, unacted)
    else:
        log('INFO', 'midday_check', 'Mid-day check: all recommendations still valid')

    return flagged


def _send_midday_alert(flagged, unacted):
    """Send Telegram alert listing flagged tickers."""
    try:
        from alerts.telegram_bot import send_alert

        lines = ['⚠️ <b>MID-DAY ALERT — Recommendations Under Review</b>\n']
        rec_by_ticker = {r['ticker']: r for r in unacted if r['ticker']}

        for ticker, reasons in flagged.items():
            rec = rec_by_ticker.get(ticker, {})
            action = rec.get('action', '?')
            conf = float(rec.get('confidence') or 0)
            lines.append(f'<b>{ticker}</b> ({action} @ conf={conf:.0%})')
            for reason in reasons[:2]:
                lines.append(f'  • {reason}')
            lines.append('')

        lines.append('ℹ️ Review on dashboard before acting on these recommendations.')
        message = '\n'.join(lines)
        send_alert(message)
        log('INFO', 'midday_check',
            f'Sent alert for {len(flagged)} flagged tickers')
    except Exception as e:
        log('ERROR', 'midday_check', f'Failed to send alert: {e}')


if __name__ == '__main__':
    flagged = run()
    if flagged:
        print(f'\n⚠️  {len(flagged)} tickers flagged:')
        for ticker, reasons in flagged.items():
            print(f'  {ticker}: {"; ".join(reasons)}')
    else:
        print('✅ All recommendations still valid at mid-day')
