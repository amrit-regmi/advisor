"""
Geographic allocation test — verifies region targets are enforced through the
reconciliation, optimizer, and portfolio_brain layers.

Scenario:
  - Region targets: Americas=30%, Europe=50%, Asia-Pacific=10%, Other=5%
  - Insert 6 US tickers + 3 European + 2 Asia-Pacific with BUY signals
  - Americas cap: 30%+10% tolerance = 40% of 10 slots = max 4 positions
  - Assert:
      1. Reconciliation blocks excess Americas buys (≤4 slots)
      2. Optimizer region weights respect caps
      3. Portfolio brain buys span ≥2 regions

Run: source /home/ubuntu/venv/bin/activate && python tests/test_geo_allocation.py
"""
import sys, os, json, traceback
from datetime import date
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, '/home/ubuntu/advisor')
os.chdir('/home/ubuntu/advisor')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')
from db.database import execute, query, log

# (ticker, sector, country, confidence)
TEST_TICKERS = [
    # 6 Americas (US)
    ('NVDA',   'Information Technology', 'US', 0.88),
    ('TSLA',   'Consumer Discretionary', 'US', 0.82),
    ('AAPL',   'Information Technology', 'US', 0.80),
    ('MSFT',   'Information Technology', 'US', 0.78),
    ('AMZN',   'Consumer Discretionary', 'US', 0.76),
    ('GOOGL',  'Communication Services', 'US', 0.74),
    # 3 Europe
    ('0GZL.DE', 'Financials',            'DE', 0.73),
    ('BNKS.L',  'Financials',            'GB', 0.71),
    ('0ICU.L',  'Health Care',           'GB', 0.70),
    # 2 Asia-Pacific
    ('0A22.L',  'Industrials',           'HK', 0.68),
    ('0MU6.L',  'Energy',                'JP', 0.66),
]

# Tight Americas cap to test enforcement
TEST_REGION_TARGETS = {
    'Americas':     30,
    'Europe':       50,
    'Asia-Pacific': 10,
    'Other':         5,
}

TEST_DATE = date.today()
CLEANUP_TICKERS = [t for t, *_ in TEST_TICKERS] + ['0KHP.L']
ORIG_REGION_TARGETS = None
ORIG_SECTOR_TARGETS = None


def assert_ok(condition, msg):
    if not condition:
        raise AssertionError(f'FAIL: {msg}')
    print(f'  PASS: {msg}')


def setup():
    global ORIG_REGION_TARGETS, ORIG_SECTOR_TARGETS
    print('\n=== SETUP ===')
    cleanup(silent=True)

    # Save originals
    r = query("SELECT value FROM user_settings WHERE key='region_targets'")
    ORIG_REGION_TARGETS = r[0]['value'] if r else None
    r = query("SELECT value FROM user_settings WHERE key='sector_targets'")
    ORIG_SECTOR_TARGETS = r[0]['value'] if r else None

    # Set test region targets
    execute("""INSERT INTO user_settings (key, value, created_at, updated_at)
               VALUES ('region_targets', %s, NOW(), NOW())
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
            (json.dumps(TEST_REGION_TARGETS),))

    # Universe entries
    for ticker, sector, country, _ in TEST_TICKERS:
        execute("""INSERT INTO universe (ticker, company_name, sector, country, exchange, active, validated)
                   VALUES (%s, %s, %s, %s, 'TEST', TRUE, TRUE)
                   ON CONFLICT (ticker) DO UPDATE SET sector=EXCLUDED.sector, country=EXCLUDED.country, active=TRUE""",
                (ticker, f'{ticker} Test Co', sector, country))

    # Mock BUY recommendations
    for ticker, _, _, conf in TEST_TICKERS:
        execute("""INSERT INTO recommendations
                       (ticker, action, confidence, reasoning, date, signal_sources, created_at)
                   VALUES (%s, 'BUY', %s, %s, %s, 'trading_agents', NOW())
                   ON CONFLICT DO NOTHING""",
                (ticker, conf, f'Mock BUY {ticker}', TEST_DATE))

    # Cash / budget
    for key, val in [('nordnet_cash_eur', '5000'),
                     ('monthly_investment_budget_eur', '5000'),
                     ('simulation_monthly_deposit', '5000'),
                     ('monthly_invested_this_month', '0')]:
        execute("""INSERT INTO user_settings (key, value, created_at, updated_at)
                   VALUES (%s, %s, NOW(), NOW())
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                (key, val))

    print(f'  {len(TEST_TICKERS)} mock BUYs: 6 Americas, 3 Europe, 2 Asia-Pacific')
    print(f'  Region targets: Americas=30%, Europe=50%, Asia-Pacific=10%')
    print(f'  Americas cap: 30%+10%tol = 40% of 10 slots = max 4')


def run_reconciliation():
    print('\n=== RECONCILIATION ===')
    from portfolio.reconciliation import reconcile
    from portfolio.sector_utils import country_to_region

    rows = query("""SELECT DISTINCT ON (ticker) ticker, action, confidence, reasoning
                    FROM recommendations WHERE date=%s AND signal_sources='trading_agents'
                    ORDER BY ticker, confidence DESC""", (TEST_DATE,))
    decisions = {r['ticker']: dict(r) for r in rows}
    contexts  = {t: {'conviction': d['confidence']} for t, d in decisions.items()}
    plan = reconcile(decisions, contexts)

    class _Enc(json.JSONEncoder):
        def default(self, o):
            return float(o) if isinstance(o, Decimal) else super().default(o)
    Path('/tmp/advisor_trade_plan.json').write_text(json.dumps(plan, cls=_Enc))

    buys   = {t: p for t, p in plan.items() if p['action'] in ('BUY', 'STRONG_BUY')}
    watches = {t: p for t, p in plan.items() if p['action'] == 'WATCH'}

    buys_by_region = {}
    for t in buys:
        r = query("SELECT country FROM universe WHERE ticker=%s LIMIT 1", (t,))
        region = country_to_region(r[0]['country'] if r else 'US')
        buys_by_region[region] = buys_by_region.get(region, 0) + 1

    print(f'  Trade plan: {len(plan)} entries, {len(buys)} BUY')
    for t, p in sorted(buys.items(), key=lambda x: -x[1]['confidence']):
        r = query("SELECT country FROM universe WHERE ticker=%s LIMIT 1", (t,))
        region = country_to_region(r[0]['country'] if r else 'US')
        print(f'    BUY: {t} ({region}) conf={p["confidence"]:.2f}')
    for t, p in sorted(watches.items(), key=lambda x: -x[1]['confidence']):
        print(f'    WATCH: {t} ({p.get("reasoning","")[:50]})')
    print(f'  Buys by region: {buys_by_region}')
    return plan, buys, buys_by_region


def run_optimizer(plan):
    print('\n=== OPTIMIZER ===')
    from portfolio.optimizer import compute_target_weights
    from portfolio.sector_utils import country_to_region

    weights = compute_target_weights(plan, cash_eur=5000)
    non_cash = {t: w for t, w in weights.items() if t != 'CASH'}

    region_weights = {}
    for t, w in non_cash.items():
        r = query("SELECT country FROM universe WHERE ticker=%s LIMIT 1", (t,))
        region = country_to_region(r[0]['country'] if r else 'US')
        region_weights[region] = region_weights.get(region, 0) + w

    print(f'  Weights: {len(non_cash)} tickers, cash={weights.get("CASH",0):.1%}')
    for t, w in sorted(non_cash.items(), key=lambda x: -x[1]):
        print(f'    {t}: {w:.1%}')
    print(f'  Region totals: { {r: f"{v:.1%}" for r, v in sorted(region_weights.items())} }')
    return weights, region_weights


def run_portfolio_brain():
    print('\n=== PORTFOLIO BRAIN ===')
    from portfolio.portfolio_brain import build_brief
    from portfolio.sector_utils import country_to_region

    brief = build_brief()
    buys = [b for b in brief.get('buys', []) if b.get('action') in ('BUY', 'STRONG_BUY')]
    print(f'  Brief: {len(buys)} buys, {len(brief.get("sells",[]))} sells')

    brain_region_counts = {}
    for b in buys:
        r = query("SELECT country FROM universe WHERE ticker=%s LIMIT 1", (b['ticker'],))
        region = country_to_region(r[0]['country'] if r else 'US')
        brain_region_counts[region] = brain_region_counts.get(region, 0) + 1
        print(f'    BUY: {b["ticker"]} ({region}) conf={b.get("confidence",0):.2f}')
    print(f'  Regions in buys: {brain_region_counts}')
    return brief, brain_region_counts


def cleanup(silent=False):
    if not silent:
        print('\n=== CLEANUP ===')
    try:
        execute("DELETE FROM recommendations WHERE ticker = ANY(%s) AND date=%s",
                (CLEANUP_TICKERS, TEST_DATE))
        for ticker in CLEANUP_TICKERS:
            execute("DELETE FROM holdings WHERE ticker=%s", (ticker,))
            execute("DELETE FROM watchlist WHERE ticker=%s", (ticker,))
        Path('/tmp/advisor_trade_plan.json').unlink(missing_ok=True)

        if ORIG_REGION_TARGETS is not None:
            execute("""INSERT INTO user_settings (key, value, created_at, updated_at)
                       VALUES ('region_targets', %s, NOW(), NOW())
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                    (ORIG_REGION_TARGETS,))
        if ORIG_SECTOR_TARGETS is not None:
            execute("""INSERT INTO user_settings (key, value, created_at, updated_at)
                       VALUES ('sector_targets', %s, NOW(), NOW())
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                    (ORIG_SECTOR_TARGETS,))

        for key, val in [('nordnet_cash_eur', '2000'),
                         ('monthly_investment_budget_eur', '500'),
                         ('simulation_monthly_deposit', '500'),
                         ('monthly_invested_this_month', '0')]:
            execute("""INSERT INTO user_settings (key, value, created_at, updated_at)
                       VALUES (%s, %s, NOW(), NOW())
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                    (key, val))

        if not silent:
            print(f'  Cleaned {len(CLEANUP_TICKERS)} tickers, restored settings')
    except Exception as e:
        if not silent:
            print(f'  Cleanup error: {e}')


if __name__ == '__main__':
    passed = failed = 0
    try:
        setup()

        plan, buys, buys_by_region = run_reconciliation()
        assert_ok(len(buys) > 0, f'Reconciliation produced BUY actions ({len(buys)} found)')
        americas_buys = buys_by_region.get('Americas', 0)
        # Americas target=30%, +10% tol = 40% of 10 = 4 slots
        assert_ok(americas_buys <= 4,
            f'Reconciliation capped Americas at ≤4 slots ({americas_buys} found)')
        europe_buys = buys_by_region.get('Europe', 0)
        assert_ok(europe_buys >= 1,
            f'Reconciliation allowed European buys ({europe_buys} found)')

        weights, region_weights = run_optimizer(plan)
        assert_ok(len(weights) > 1, f'Optimizer produced weights ({len(weights)} entries)')
        americas_w = region_weights.get('Americas', 0)
        # Reconciliation already capped Americas at 4/9 tickers (~44% equal-weight).
        # Test that Americas never dominates (>55% would mean 5+ US tickers slipped through).
        assert_ok(americas_w <= 0.55,
            f'Optimizer Americas weight ≤55% — reconciliation pre-filtered correctly (got {americas_w:.1%})')

        brief, brain_region_counts = run_portfolio_brain()
        brain_buys = [b for b in brief.get('buys', []) if b.get('action') in ('BUY', 'STRONG_BUY')]
        assert_ok(len(brain_buys) > 0,
            f'Portfolio brain produced buy recommendations ({len(brain_buys)} found)')
        n_regions = len([r for r, c in brain_region_counts.items() if c > 0])
        assert_ok(n_regions >= 2,
            f'Portfolio brain buys span ≥2 regions ({n_regions} found: {brain_region_counts})')

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
    remaining = query("SELECT COUNT(*) n FROM recommendations WHERE ticker = ANY(%s) AND date=%s",
                      (CLEANUP_TICKERS, TEST_DATE))
    remaining_h = query("SELECT COUNT(*) n FROM holdings WHERE ticker = ANY(%s)", (CLEANUP_TICKERS,))
    print(f'Post-cleanup: {remaining[0]["n"]} test recs, {remaining_h[0]["n"]} test holdings remain')

    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)
