"""
Sector allocation test — verifies that the pipeline enforces user-configured
sector targets throughout reconciliation, optimizer, and portfolio_brain.

Scenario:
  - Sector targets: Technology=20%, Financial Services=20%, Healthcare=20%,
    Consumer Cyclical=10%, Energy=5% (sum=75%; rest cash)
  - Technology capped at 20% → max 2 positions out of 10
  - Insert 5 Tech tickers + 3 Finance + 2 Healthcare with BUY signals
  - Assert:
      1. Reconciliation blocks excess Technology buys (≤2 Tech slots)
      2. Optimizer weights respect sector caps
      3. portfolio_brain brief contains cross-sector buys, not just Tech

Run: source /home/ubuntu/venv/bin/activate && python tests/test_sector_allocation.py
"""
import sys
import os
import json
import traceback
from datetime import date
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, '/home/ubuntu/advisor')
os.chdir('/home/ubuntu/advisor')

from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')
from db.database import execute, query, log

# ── Test tickers — spread across sectors ──────────────────────────────────────
# (ticker, universe_sector, country, confidence)
TEST_TICKERS = [
    # 5 Technology — should be capped at ≤2 by reconciliation (target 20% → 2/10)
    ('NVDA',    'Information Technology', 'US', 0.85),
    ('TSLA',    'Information Technology', 'US', 0.80),
    ('14D.DE',  'Information Technology', 'DE', 0.75),
    ('0A22.L',  'Information Technology', 'GB', 0.70),
    ('0IN2.L',  'Information Technology', 'GB', 0.68),
    # 3 Financial Services
    ('0GZL.DE', 'Financials',             'DE', 0.72),
    ('BNKS.L',  'Financials',             'GB', 0.69),
    ('0LBM.L',  'Financials',             'GB', 0.65),
    # 2 Healthcare
    ('0ICU.L',  'Health Care',            'GB', 0.74),
    ('0MU6.L',  'Health Care',            'GB', 0.66),
]

TEST_SECTORS = {
    'Technology':        20,
    'Financial Services': 20,
    'Healthcare':        20,
    'Consumer Cyclical': 10,
    'Consumer Defensive': 5,
    'Communication Services': 5,
    'Energy':             5,
    'Industrials':        5,
    'Basic Materials':    0,
    'Real Estate':        0,
    'Utilities':          0,
}

TEST_DATE = date.today()
CLEANUP_TICKERS = [t for t, *_ in TEST_TICKERS]
ORIG_TARGETS = None  # saved before test, restored after


def assert_ok(condition, msg):
    if not condition:
        raise AssertionError(f'FAIL: {msg}')
    print(f'  PASS: {msg}')


def setup():
    global ORIG_TARGETS
    print('\n=== SETUP ===')
    cleanup(silent=True)

    # Save original sector targets
    row = query("SELECT value FROM user_settings WHERE key='sector_targets'")
    ORIG_TARGETS = row[0]['value'] if row else None

    # Set test sector targets
    execute("""
        INSERT INTO user_settings (key, value, created_at, updated_at)
        VALUES ('sector_targets', %s, NOW(), NOW())
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
    """, (json.dumps(TEST_SECTORS),))

    # Ensure universe entries exist
    for ticker, sector, country, _ in TEST_TICKERS:
        execute("""
            INSERT INTO universe (ticker, company_name, sector, country, exchange, active, validated)
            VALUES (%s, %s, %s, %s, 'TEST', TRUE, TRUE)
            ON CONFLICT (ticker) DO UPDATE SET sector=EXCLUDED.sector, country=EXCLUDED.country, active=TRUE
        """, (ticker, f'{ticker} Test Co', sector, country))

    # Insert mock BUY recommendations
    for ticker, _, _, conf in TEST_TICKERS:
        execute("""
            INSERT INTO recommendations
                (ticker, action, confidence, reasoning, date, signal_sources, created_at)
            VALUES (%s, 'BUY', %s, %s, %s, 'trading_agents', NOW())
            ON CONFLICT DO NOTHING
        """, (ticker, conf, f'Mock BUY signal for {ticker}', TEST_DATE))

    # Cash — set both real and simulation budgets high so all 3 sectors can be bought
    for key, val in [('nordnet_cash_eur', '5000'),
                     ('monthly_investment_budget_eur', '5000'),
                     ('simulation_monthly_deposit', '5000'),
                     ('monthly_invested_this_month', '0')]:
        execute("""
            INSERT INTO user_settings (key, value, created_at, updated_at)
            VALUES (%s, %s, NOW(), NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        """, (key, val))
    print(f'  Inserted {len(TEST_TICKERS)} mock BUY recommendations across 3 sectors')
    print(f'  Sector targets: Tech=20%, Finance=20%, Healthcare=20%')
    print(f'  Tech tickers: 5 inserted → reconciliation should allow max 2')


def run_reconciliation():
    print('\n=== RECONCILIATION ===')
    from portfolio.reconciliation import reconcile

    rows = query("""
        SELECT DISTINCT ON (ticker) ticker, action, confidence, reasoning
        FROM recommendations
        WHERE date=%s AND signal_sources='trading_agents'
        ORDER BY ticker, confidence DESC
    """, (TEST_DATE,))

    decisions = {r['ticker']: dict(r) for r in rows}
    contexts  = {t: {'conviction': d['confidence']} for t, d in decisions.items()}
    plan = reconcile(decisions, contexts)

    class _Enc(json.JSONEncoder):
        def default(self, o):
            return float(o) if isinstance(o, Decimal) else super().default(o)
    Path('/tmp/advisor_trade_plan.json').write_text(json.dumps(plan, cls=_Enc))

    buys = {t: p for t, p in plan.items() if p['action'] in ('BUY', 'STRONG_BUY')}
    watches = {t: p for t, p in plan.items() if p['action'] == 'WATCH'}

    # Count by sector
    from portfolio.sector_utils import normalize
    buys_by_sector = {}
    for t in buys:
        row = query("SELECT sector FROM universe WHERE ticker=%s LIMIT 1", (t,))
        sec = normalize(row[0]['sector'] if row else '') if row else 'Unknown'
        buys_by_sector[sec] = buys_by_sector.get(sec, 0) + 1

    print(f'  Trade plan: {len(plan)} entries, {len(buys)} BUY')
    for t, p in sorted(buys.items(), key=lambda x: -x[1]['confidence']):
        print(f'    BUY: {t} conf={p["confidence"]:.2f}')
    for t, p in sorted(watches.items(), key=lambda x: -x[1]['confidence']):
        print(f'    WATCH: {t} ({p.get("reasoning","")[:40]})')
    print(f'  Buys by sector: {buys_by_sector}')
    return plan, buys, buys_by_sector


def run_optimizer(plan):
    print('\n=== OPTIMIZER ===')
    from portfolio.optimizer import compute_target_weights

    weights = compute_target_weights(plan, cash_eur=5000)
    non_cash = {t: w for t, w in weights.items() if t != 'CASH'}

    # Compute weight by sector
    from portfolio.sector_utils import normalize
    sector_weights = {}
    for t, w in non_cash.items():
        row = query("SELECT sector FROM universe WHERE ticker=%s LIMIT 1", (t,))
        sec = normalize(row[0]['sector'] if row else '') if row else 'Unknown'
        sector_weights[sec] = sector_weights.get(sec, 0) + w

    print(f'  Weights: {len(non_cash)} tickers, cash={weights.get("CASH", 0):.1%}')
    for t, w in sorted(non_cash.items(), key=lambda x: -x[1]):
        print(f'    {t}: {w:.1%}')
    print(f'  Sector totals: { {s: f"{v:.1%}" for s, v in sorted(sector_weights.items())} }')
    return weights, sector_weights


def run_portfolio_brain():
    print('\n=== PORTFOLIO BRAIN ===')
    from portfolio.portfolio_brain import build_brief
    brief = build_brief()

    buys = [b for b in brief.get('buys', []) if b.get('action') in ('BUY', 'STRONG_BUY')]
    print(f'  Brief: {len(buys)} buys, {len(brief.get("sells", []))} sells')

    from portfolio.sector_utils import normalize
    brain_sector_counts = {}
    for b in buys:
        sec = normalize(b.get('sector', ''))
        brain_sector_counts[sec] = brain_sector_counts.get(sec, 0) + 1
        print(f'    BUY: {b["ticker"]} sector={sec} conf={b.get("confidence",0):.2f}')

    print(f'  Sectors in buys: {brain_sector_counts}')
    return brief, brain_sector_counts


def cleanup(silent=False):
    if not silent:
        print('\n=== CLEANUP ===')
    try:
        # Remove all recommendations for test tickers (any source — portfolio_brain also writes them)
        execute("DELETE FROM recommendations WHERE ticker = ANY(%s) AND date=%s",
                (CLEANUP_TICKERS, TEST_DATE))
        # Also remove 0KHP.L which portfolio_brain may add from discovery during the test
        execute("DELETE FROM recommendations WHERE ticker='0KHP.L' AND date=%s", (TEST_DATE,))
        for ticker in CLEANUP_TICKERS + ['0KHP.L']:
            execute("DELETE FROM holdings WHERE ticker=%s", (ticker,))
            execute("DELETE FROM watchlist WHERE ticker=%s", (ticker,))
        Path('/tmp/advisor_trade_plan.json').unlink(missing_ok=True)

        # Restore original sector targets
        if ORIG_TARGETS is not None:
            execute("""
                INSERT INTO user_settings (key, value, created_at, updated_at)
                VALUES ('sector_targets', %s, NOW(), NOW())
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """, (ORIG_TARGETS,))

        # Restore cash / budgets
        for key, val in [('nordnet_cash_eur', '2000'),
                         ('monthly_investment_budget_eur', '500'),
                         ('simulation_monthly_deposit', '500'),
                         ('monthly_invested_this_month', '0')]:
            execute("""
                INSERT INTO user_settings (key, value, created_at, updated_at)
                VALUES (%s, %s, NOW(), NOW())
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """, (key, val))

        if not silent:
            print(f'  Cleaned {len(CLEANUP_TICKERS)} tickers, restored sector targets + cash')
    except Exception as e:
        if not silent:
            print(f'  Cleanup error: {e}')


if __name__ == '__main__':
    passed = failed = 0

    try:
        setup()

        # ── 1. Reconciliation sector cap ──────────────────────────────────────
        plan, buys, buys_by_sector = run_reconciliation()

        assert_ok(len(buys) > 0, f'Reconciliation produced BUY actions ({len(buys)} found)')

        tech_buys = buys_by_sector.get('Technology', 0)
        # Tech target=20% of 10 max holdings = 2 slots (+5% tolerance = still 2)
        assert_ok(tech_buys <= 2,
            f'Reconciliation capped Technology buys at ≤2 ({tech_buys} found)')

        fin_buys = buys_by_sector.get('Financial Services', 0)
        assert_ok(fin_buys >= 1,
            f'Reconciliation allowed Financial Services buys ({fin_buys} found)')

        # ── 2. Optimizer sector weights ───────────────────────────────────────
        weights, sector_weights = run_optimizer(plan)

        assert_ok(len(weights) > 1, f'Optimizer produced weights ({len(weights)} entries)')

        tech_weight = sector_weights.get('Technology', 0)
        # Cap is target(20%) + 5% tolerance = 25%
        assert_ok(tech_weight <= 0.26,
            f'Optimizer Technology weight ≤26% (got {tech_weight:.1%})')

        # ── 3. Portfolio brain cross-sector buys ──────────────────────────────
        brief, brain_sector_counts = run_portfolio_brain()

        brain_buys = [b for b in brief.get('buys', []) if b.get('action') in ('BUY', 'STRONG_BUY')]
        assert_ok(len(brain_buys) > 0,
            f'Portfolio brain produced buy recommendations ({len(brain_buys)} found)')

        n_sectors_represented = len([s for s, c in brain_sector_counts.items()
                                     if s not in ('Unknown', '') and c > 0])
        assert_ok(n_sectors_represented >= 2,
            f'Portfolio brain buys span ≥2 sectors ({n_sectors_represented} found)')

        passed = 6
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

    # Verify cleanup
    remaining = query(
        "SELECT COUNT(*) n FROM recommendations WHERE date=%s AND signal_sources='trading_agents'",
        (TEST_DATE,)
    )
    remaining_h = query("SELECT COUNT(*) n FROM holdings WHERE ticker = ANY(%s)", (CLEANUP_TICKERS,))
    print(f'Post-cleanup: {remaining[0]["n"]} test recommendations, {remaining_h[0]["n"]} test holdings remain')

    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)
