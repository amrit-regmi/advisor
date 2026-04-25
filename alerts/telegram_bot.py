"""
Telegram bot — sends morning brief and urgent alerts.
"""
import sys
import os
import requests
from datetime import date
from dotenv import load_dotenv

load_dotenv('/home/ubuntu/advisor/.env')
sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import log

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
TELEGRAM_API = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}'


def _send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log('WARNING', 'telegram', 'No Telegram credentials — printing to stdout')
        print(text)
        return False

    if TELEGRAM_TOKEN == 'your_bot_token_here' or TELEGRAM_CHAT_ID == 'your_chat_id_here':
        log('WARNING', 'telegram', 'Placeholder credentials — printing to stdout')
        print(text)
        return False

    try:
        resp = requests.post(
            f'{TELEGRAM_API}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=30
        )
        if resp.status_code == 200:
            log('INFO', 'telegram', 'Message sent successfully')
            return True
        else:
            log('ERROR', 'telegram', f'Send failed: {resp.status_code} {resp.text[:100]}')
            return False
    except Exception as e:
        log('ERROR', 'telegram', f'Send error: {e}')
        return False


def _format_brief(brief):
    today = brief['date']
    pv = brief.get('portfolio_value_eur', 0)
    cash = brief.get('cash_eur', 0)
    freed = brief.get('freed_cash_eur', 0)
    fresh = brief.get('fresh_capital_needed', 0)

    lines = [
        f'📊 <b>ADVISOR BRIEF — {today}</b>',
        f'Portfolio: €{pv:,.0f} | Cash: €{cash:,.0f}',
        '',
    ]

    # SELL section
    sells = brief.get('sells', [])
    if sells:
        lines.append('🔴 <b>SELL</b>')
        for s in sells:
            exit_type = 'Full exit' if s.get('full_exit') else f"Sell {s['shares']} of {s['shares_held']:.0f}"
            lines.append(
                f"<b>{s['ticker']}</b> — {exit_type} shares "
                f"(~€{s['proceeds_eur']:,.0f})"
            )
            # 2-3 sentence reasoning
            reasoning = s['reasoning'][:300].replace('\n', ' ')
            lines.append(reasoning)
            lines.append('')
    else:
        lines.append('🔴 No sell signals today')
        lines.append('')

    # BUY section
    buys = brief.get('buys', [])
    if buys:
        lines.append('🟢 <b>BUY</b>')
        for b in buys:
            lines.append(
                f"<b>{b['ticker']}</b> — {b['shares']} shares "
                f"@ €{b['price_eur']:.2f} (~€{b['total_cost_eur']:,.0f})"
            )
            lines.append(f"Via: {b['funding_source']}")
            reasoning = b['reasoning'][:250].replace('\n', ' ')
            lines.append(reasoning)
            if b.get('tax_notes'):
                lines.append(f"💶 {b['tax_notes'][:100]}")
            lines.append('')
    else:
        lines.append('🟢 No buy signals meeting threshold today')
        lines.append('')

    # WATCH section
    watches = brief.get('watches', [])
    if watches:
        lines.append('👁 <b>WATCHING</b>')
        for w in watches[:5]:
            lines.append(f"<b>{w['ticker']}</b> — conf={w['confidence']:.0%}")
            lines.append(f"Triggers BUY when: {w.get('trigger', 'conditions improve')}")
            lines.append('')

    # Funding summary
    lines.append('💰 <b>FUNDING</b>')
    lines.append(f"Cash: €{cash:,.0f} | From sells: €{freed:,.0f}")
    if fresh > 0:
        lines.append(f"Fresh capital suggested: €{fresh:,.0f}")

    return '\n'.join(lines)


def send_brief(brief):
    """Format and send the morning brief."""
    text = _format_brief(brief)
    log('INFO', 'telegram', f'Sending morning brief ({len(text)} chars)')
    return _send(text)


def send_alert(message):
    """Send an urgent intraday alert."""
    text = f'⚠️ <b>ADVISOR ALERT</b>\n{message}'
    return _send(text)


def send_test():
    """Send a test message to verify bot is working."""
    text = (f'✅ Advisor bot is working!\n'
            f'Date: {date.today()}\n'
            f'System is running correctly.')
    return _send(text)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        send_test()
    else:
        # Send actual brief
        sys.path.insert(0, '/home/ubuntu/advisor')
        from portfolio.portfolio_brain import build_brief
        brief = build_brief()
        text = _format_brief(brief)
        print(text)  # Always print to stdout
        send_brief(brief)
