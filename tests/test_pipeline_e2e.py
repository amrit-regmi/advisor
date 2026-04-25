"""
End-to-end pipeline test with mocked TradingAgents decisions.

Tests: recommendations → reconciliation → optimizer → portfolio_brain → auto_executor
Simulates 10 BUY signals and verifies that holdings are actually created.
Cleans up all test data on completion (pass or fail).

Run: source /home/ubuntu/venv/bin/activate && python tests/test_pipeline_e2e.py
"""
import sys
import os
import json
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/advisor')
os.chdir('/home/ubuntu/advisor')

from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')
from db.database import execute, query, log

# ── Test tickers (have plenty of price history) ────────────────────────────────
TEST_TICKERS = [
    ('NVDA',    'Technology',       'US',  0.72),
    ('TSLA',    'Consumer',         'US',  0.65),
    ('0GZL.DE', 'Financials',       'DE',  0.68),
    ('BNKS.L',  'Financials',       'GB',  0.64),
    ('0ICU.L',  'Healthcare',       'GB',  0.63),
    ('0MU6.L',  'Energy',           'GB',  0.61),
    ('0A22.L',  'Industrials',      'GB',  0.60),
    ('14D.DE',  'Technology',       'DE',  0.66),
    ('0IN2.L',  'Materials',        'GB',  0.62),
    ('0LBM.L',  'Staples',          'GB',  0.60),
]

TEST_DATE = date.today()
CLEANUP_TICKERS = [t for t, *_ in TEST_TICKERS]


def setup():
    """Insert mock TA recommendations and ensure universe entries exist."""
    print('\n=== SETUP ===')
    # Clear any existing test data
    cleanup(silent=True)

    # Ensure universe entries exist for test tickers
    for ticker, sector, country, conf in TEST_TICKERS:
        execute("""
            INSERT INTO universe (ticker, company_name, sector, country, exchange, active, validated)
            VALUES (%s, %s, %s, %s, 'TEST', TRUE, TRUE)
            ON CONFLICT (ticker) DO UPDATE SET
                sector=EXCLUDED.sector, country=EXCLUDED.country, active=TRUE
        """, (ticker, f'{ticker} Test Co', sector, country))

    # Insert mock BUY recommendations
    for ticker, sector, country, conf in TEST_TICKERS:
        execute("""
            INSERT INTO recommendations
                (ticker, action, confidence, reasoning, date, signal_sources, created_at)
            VALUES (%s, 'BUY', %s, %s, %s, 'trading_agents', NOW())
            ON CONFLICT DO NOTHING
        """, (ticker, conf,
              f'Mock BUY signal for {ticker}: strong momentum and positive sentiment',
              TEST_DATE))

    count = query("SELECT COUNT(*) AS n FROM recommendations WHERE date=%s AND signal_sources='trading_agents'", (TEST_DATE,))[0]['n']
    print(f'  Inserted {count} mock BUY recommendations')

    # Set cash in user_settings if not present
    execute("""
        INSERT INTO user_settings (key, value, created_at, updated_at)
        VALUES ('nordnet_cash_eur', '2000', NOW(), NOW())
        ON CONFLICT (key) DO UPDATE SET value='2000', updated_at=NOW()
    """)
    print('  Cash set to €2000')


def run_reconciliation():
    """Run reconciliation with today's mock recommendations."""
    print('\n=== RECONCILIATION ===')
    from portfolio.reconciliation import reconcile

    rows = query("""
        SELECT DISTINCT ON (ticker) ticker, action, confidence, reasoning, bull_case, bear_case, key_risks
        FROM recommendations
        WHERE date=%s AND signal_sources='trading_agents'
        ORDER BY ticker, confidence DESC
    """, (TEST_DATE,))

    decisions = {r['ticker']: dict(r) for r in rows}
    contexts  = {t: {'conviction': d['confidence']} for t, d in decisions.items()}
    plan = reconcile(decisions, contexts)

    from decimal import Decimal
    class _Enc(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, Decimal): return float(o)
            return super().default(o)
    Path('/tmp/advisor_trade_plan.json').write_text(json.dumps(plan, cls=_Enc))

    buy_count = sum(1 for p in plan.values() if p['action'] in ('BUY', 'STRONG_BUY'))
    print(f'  Trade plan: {len(plan)} entries, {buy_count} BUY/STRONG_BUY')
    for t, p in sorted(plan.items(), key=lambda x: -x[1].get('confidence', 0)):
        if p['action'] not in ('HOLD', 'AVOID', 'WATCH'):
            print(f'    {t}: {p["action"]} conf={p["confidence"]:.2f}')
    return plan


def run_optimizer(plan):
    """Run optimizer on the trade plan."""
    print('\n=== OPTIMIZER ===')
    from portfolio.optimizer import compute_target_weights

    buy_tickers = [t for t, p in plan.items() if p['action'] in ('BUY', 'STRONG_BUY')]
    if not buy_tickers:
        print('  No buy tickers — skipping optimizer')
        return {}

    weights = compute_target_weights(plan, cash_eur=2000)
    non_cash = {t: w for t, w in weights.items() if t != 'CASH'}
    print(f'  Weights: {len(non_cash)} tickers, cash={weights.get("CASH", 0):.1%}')
    for t, w in sorted(non_cash.items(), key=lambda x: -x[1]):
        print(f'    {t}: {w:.1%}')
    return weights


def run_portfolio_brain():
    """Run portfolio_brain to produce final brief and execute buys."""
    print('\n=== PORTFOLIO BRAIN ===')
    from portfolio.portfolio_brain import build_brief
    brief = build_brief()
    buys  = [b for b in brief.get('buys', []) if b.get('action') in ('BUY', 'STRONG_BUY')]
    watches = brief.get('watches', [])
    print(f'  Brief: {len(buys)} buys, {len(brief.get("sells", []))} sells, {len(watches)} watches')
    for b in buys:
        print(f'    BUY: {b["ticker"]} conf={b.get("confidence", 0):.2f} '
              f'shares={b.get("shares_to_buy", "?")} cost=€{b.get("cost_eur", 0):.0f}')
    return brief


def check_holdings():
    """Verify holdings were created."""
    print('\n=== HOLDINGS CHECK ===')
    holdings = query("""
        SELECT ticker, shares, avg_buy_price, currency
        FROM holdings WHERE active=TRUE AND shares>0 AND ticker = ANY(%s)
    """, (CLEANUP_TICKERS,))
    print(f'  Test holdings created: {len(holdings)}')
    for h in holdings:
        print(f'    {h["ticker"]}: {h["shares"]} shares @ {h["avg_buy_price"]} {h["currency"]}')
    return len(holdings)


def run_auto_executor(brief):
    """Run auto-executor to simulate trade execution."""
    print('\n=== AUTO EXECUTOR ===')
    try:
        from portfolio.auto_executor import run as auto_run
        auto_run(brief)
        print('  Auto-executor completed')
    except Exception as e:
        print(f'  Auto-executor error: {e}')


def cleanup(silent=False):
    """Remove all test data — call after test regardless of result."""
    if not silent:
        print('\n=== CLEANUP ===')
    try:
        # Remove all recommendations for test tickers (trading_agents + portfolio_brain writes)
        execute("DELETE FROM recommendations WHERE ticker = ANY(%s) AND date=%s",
                (CLEANUP_TICKERS, TEST_DATE))
        # Also clean 0KHP.L which portfolio_brain may add from discovery
        execute("DELETE FROM recommendations WHERE ticker='0KHP.L' AND date=%s", (TEST_DATE,))
        for ticker in CLEANUP_TICKERS + ['0KHP.L']:
            execute("DELETE FROM holdings WHERE ticker=%s", (ticker,))
            execute("DELETE FROM watchlist WHERE ticker=%s", (ticker,))
        # Remove test trade_plan file
        Path('/tmp/advisor_trade_plan.json').unlink(missing_ok=True)
        if not silent:
            print(f'  Removed test data for {len(CLEANUP_TICKERS)} tickers')
    except Exception as e:
        if not silent:
            print(f'  Cleanup error: {e}')


def assert_ok(condition, msg):
    if not condition:
        raise AssertionError(f'FAIL: {msg}')
    print(f'  PASS: {msg}')


if __name__ == '__main__':
    passed = 0
    failed = 0

    try:
        setup()

        plan = run_reconciliation()
        assert_ok(len(plan) > 0, f'Reconciliation produced a trade plan ({len(plan)} entries)')
        buy_count = sum(1 for p in plan.values() if p['action'] in ('BUY', 'STRONG_BUY'))
        assert_ok(buy_count > 0, f'Reconciliation has at least 1 BUY ({buy_count} found)')

        weights = run_optimizer(plan)
        investable = {t: w for t, w in weights.items() if t != 'CASH' and w > 0}
        assert_ok(len(weights) > 0, f'Optimizer produced weights ({len(weights)} entries)')
        assert_ok(weights.get('CASH', 0) < 0.98, f'Optimizer allocates to equities (cash={weights.get("CASH",1):.0%})')

        brief = run_portfolio_brain()
        buys = [b for b in brief.get('buys', []) if b.get('action') in ('BUY', 'STRONG_BUY')]
        assert_ok(len(buys) > 0, f'Portfolio brain produced buy recommendations ({len(buys)} found)')

        run_auto_executor(brief)

        n_holdings = check_holdings()
        assert_ok(n_holdings > 0, f'Auto-executor created at least 1 holding ({n_holdings} found)')

        passed = 5
    except AssertionError as e:
        print(f'\n{e}')
        failed += 1
    except Exception as e:
        print(f'\nUNEXPECTED ERROR: {e}')
        traceback.print_exc()
        failed += 1
    finally:
        cleanup()

    print(f'\n=== RESULTS: {passed} passed, {failed} failed ===')
    sys.exit(0 if failed == 0 else 1)
