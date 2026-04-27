"""
Flask web dashboard — portfolio overview and management.
Binds to 0.0.0.0:5000 for external access.
"""
import os
import sys
import socket
import queue
import threading
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')
from flask import (Flask, render_template_string, request,
                   redirect, url_for, jsonify, flash)

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, execute, log

app = Flask(__name__)
app.secret_key = 'advisor-dash-2025'

# ── Analysis queue — single worker, FIFO ────────────────────────────────────
# All "Analyze Now" requests go here; the worker processes one at a time.
_analysis_queue = queue.Queue()          # items: ticker strings
_analysis_lock  = threading.Lock()       # guards _analysis_state
DEBATE_STEPS = [
    (1, 'Signal Engine',              'Computing conviction score'),
    (2, 'Groq agents (7)',            'Market + News + Bull/Bear×4 + Evaluator + Trader'),
    (3, 'OR Risk Analyst',            'Nvidia Nemotron risk assessment'),
    (4, 'OR Portfolio Manager',       'Final decision: ACTION / CONFIDENCE / REASONING'),
]

_analysis_state = {
    'active':  None,   # ticker currently being analyzed
    'queue':   [],     # ordered list of tickers waiting
    'debate':  {},     # {ticker: {step_active: int, steps_done: [{num,name,text}]}}
}


def _analysis_worker():
    """Single background thread — drains the queue one ticker at a time.
    Catches all exceptions so the thread never dies silently; the watchdog
    below will restart it if something truly unrecoverable happens.
    """
    while True:
        try:
            ticker = _analysis_queue.get(timeout=5)
        except queue.Empty:
            continue
        with _analysis_lock:
            _analysis_state['active'] = ticker
            if ticker in _analysis_state['queue']:
                _analysis_state['queue'].remove(ticker)
        try:
            _run_analysis_background(ticker)
        except Exception as e:
            log('ERROR', 'trading_agents', f'[ON-DEMAND] Worker error for {ticker}: {e}')
        finally:
            with _analysis_lock:
                _analysis_state['active'] = None
            try:
                _analysis_queue.task_done()
            except ValueError:
                pass  # task_done() called more times than get() — harmless


def _start_worker():
    t = threading.Thread(target=_analysis_worker, daemon=True, name='analysis-worker')
    t.start()
    return t


_analysis_worker_thread = _start_worker()


def _worker_watchdog():
    """Restart the analysis worker if it ever dies unexpectedly."""
    global _analysis_worker_thread
    while True:
        threading.Event().wait(timeout=15)
        if not _analysis_worker_thread.is_alive():
            log('WARNING', 'dashboard', 'Analysis worker thread died — restarting')
            _analysis_worker_thread = _start_worker()


threading.Thread(target=_worker_watchdog, daemon=True, name='worker-watchdog').start()

_FX_CACHE = {}
_FX_LOADED_AT = None

def get_live_fx():
    """Load FX rates from macro_data; refresh at most once per hour."""
    global _FX_CACHE, _FX_LOADED_AT
    now = datetime.now()
    if _FX_CACHE and _FX_LOADED_AT and (now - _FX_LOADED_AT).seconds < 3600:
        return _FX_CACHE
    static = {'EUR': 1.0, 'USD': 0.853, 'GBP': 1.17, 'JPY': 0.0063,
              'AUD': 0.59, 'SEK': 0.078, 'CHF': 1.06, 'HKD': 0.11}
    try:
        rows = query("""
            SELECT DISTINCT ON (series_id) series_id, value FROM macro_data
            WHERE series_id IN ('DEXUSEU','DEXUSUK','DEXJPUS','DEXUSAL',
                                'DEXSDUS','DEXSZUS','DEXHKUS','DEXDNUS')
            AND date >= CURRENT_DATE - INTERVAL '30 days'
            ORDER BY series_id, date DESC
        """)
        seen = {r['series_id']: float(r['value']) for r in rows if r['value'] is not None}
        if seen:
            usd_per_eur = seen.get('DEXUSEU', 1.17)
            eur_per_usd = 1.0 / usd_per_eur
            _FX_CACHE = {
                'EUR': 1.0, 'USD': eur_per_usd,
                'GBP': seen.get('DEXUSUK', 1.37) / usd_per_eur,
                'JPY': eur_per_usd / seen['DEXJPUS'] if 'DEXJPUS' in seen else 0.0063,
                'AUD': eur_per_usd / seen['DEXUSAL'] if 'DEXUSAL' in seen else 0.59,
                'SEK': eur_per_usd / seen['DEXSDUS'] if 'DEXSDUS' in seen else 0.078,
                'CHF': eur_per_usd / seen['DEXSZUS'] if 'DEXSZUS' in seen else 1.06,
                'HKD': eur_per_usd / seen['DEXHKUS'] if 'DEXHKUS' in seen else 0.11,
            }
            _FX_LOADED_AT = now
            return _FX_CACHE
    except Exception:
        pass
    _FX_CACHE = static
    _FX_LOADED_AT = now
    return static

FX = static = {'USD': 0.853, 'GBP': 1.17, 'EUR': 1.0, 'SEK': 0.078,
               'JPY': 0.0063, 'CHF': 1.06, 'HKD': 0.11, 'AUD': 0.59}

SETTINGS_META = [
    ('risk_level',                     'Risk Level',                                'select',   'medium'),
    ('alert_time',                     'Alert Time',                                'time',     '07:00'),
    ('max_stocks_per_day',             'Max Stocks Per Day (TradingAgents)',        'number',   '5'),
    ('min_confidence',                 'Min Confidence Threshold (0-1)',            'number',   '0.6'),
    ('stop_loss_pct',                  'Stop-Loss % (e.g. 15)',                     'number',   '15'),
    ('rebalance_threshold_pct',        'Rebalance Threshold % (e.g. 7)',            'number',   '7'),
]

STAGE_COMPONENTS = [
    ('Data Collection',  ['collector', 'gdelt', 'fred', 'polymarket', 'prices']),
    ('Universe Loader',  ['universe']),
    ('Event Classifier', ['event_classifier']),
    ('Event Mapper',     ['event_mapper']),
    ('Scorer',           ['scorer']),
    ('Qlib/Factor',      ['qlib_scorer']),
    ('Tax Filter',       ['tax_filter']),
    ('TradingAgents',    ['trading_agents']),
    ('Portfolio Brain',  ['portfolio_brain']),
    ('Sanity Check',     ['sanity_check']),
    ('Perf Tracker',     ['performance_tracker']),
    ('Telegram Brief',   ['telegram', 'telegram_bot']),
    ('Mid-day Check',    ['midday_check']),
]

MACRO_NAMES = {
    'DFF': 'Fed Funds Rate', 'T10Y2Y': 'Yield Curve (10y-2y)',
    'CPIAUCSL': 'CPI Inflation', 'VIXCLS': 'VIX',
    'DEXUSEU': 'EUR/USD (USD per EUR)', 'DCOILWTICO': 'WTI Crude Oil',
    'UNRATE': 'US Unemployment Rate', 'T10YIE': 'Inflation Expectations (10y)',
    'BAMLH0A0HYM2': 'High Yield Credit Spread', 'DEXUSUK': 'GBP/USD',
}

MACRO_DESC = {
    'DFF':          ('Federal Funds Rate — the overnight lending rate set by the Fed.',
                     'Rising rates → favour banks/financials, hurt growth/REITs/bonds.'),
    'T10Y2Y':       ('10yr minus 2yr Treasury yield spread.',
                     'Negative (inverted) = recession signal. Positive = growth expected.'),
    'CPIAUCSL':     ('Consumer Price Index — broad inflation measure.',
                     'High CPI → Fed raises rates → pressure on equities, especially growth.'),
    'VIXCLS':       ('CBOE Volatility Index — "fear gauge" of the S&P 500.',
                     '<20 = calm; >30 = fear; >40 = crisis. High VIX = risk-off.'),
    'DEXUSEU':      ('USD per EUR exchange rate.',
                     'Rising = stronger EUR vs USD. Affects European exporters negatively.'),
    'DCOILWTICO':   ('West Texas Intermediate crude oil price (USD/barrel).',
                     'Rising oil → good for energy stocks, bad for airlines/chemicals/consumer.'),
    'UNRATE':       ('US Unemployment Rate.',
                     'Low unemployment → Fed keeps rates high → pressure on bonds.'),
    'T10YIE':       ('10-year inflation breakeven — market inflation expectations.',
                     'Rising = investors expect higher inflation → favour TIPS and commodities.'),
    'BAMLH0A0HYM2': ('High Yield (junk bond) credit spread over Treasuries.',
                     'Widening spread = credit stress. >6% = risk-off; watch banking/credit.'),
    'DEXUSUK':      ('USD per GBP exchange rate.',
                     'Affects UK stocks for EUR-based investors; watch post-Brexit policy.'),
}

NAV_ITEMS = [
    ('/', 'Today'), ('/research', 'Research'), ('/pipeline', 'Pipeline'),
    ('/history', 'History'), ('/settings', 'Settings'),
]



# ── UX helpers ───────────────────────────────────────────────────────────────

def _natural_summary(rec):
    """Strip noise from reasoning and return a clean 1-2 sentence summary."""
    text = rec.get('reasoning') or ''
    # Strip pipeline annotations
    for tag in ['[ON-DEMAND]', '[MOCK]', 'Mock analysis', 'mock analysis']:
        text = text.replace(tag, '').strip()
    # Take first 2 sentences (up to 300 chars)
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    summary = ' '.join(sentences[:2])[:300].strip()
    if not summary or len(summary) < 20:
        action = rec.get('action', 'HOLD')
        ticker = rec.get('ticker', '')
        conf = int((rec.get('confidence') or 0.5) * 100)
        summary = f"{action} signal based on combined GDELT, macro, and price momentum analysis. {conf}% confidence."
    return summary


def _get_market_regime():
    """Derive simple market regime from latest macro data."""
    try:
        macro = query("""
            SELECT DISTINCT ON (series_id) series_id, value FROM macro_data
            WHERE series_id IN ('VIXCLS','T10Y2Y','DFF')
            ORDER BY series_id, date DESC
        """)
        m = {r['series_id']: float(r['value'] or 0) for r in macro}
        vix   = m.get('VIXCLS', 20)
        curve = m.get('T10Y2Y', 0)
        if vix > 30 or curve < -0.5:
            return 'risk-off', '⚡', 'Risk-Off', 'High volatility or inverted yield curve — favour defensive positions'
        elif vix > 22:
            return 'bearish', '⚠', 'Caution', 'Elevated volatility — size positions conservatively'
        elif curve > 0.5 and vix < 18:
            return 'bullish', '📈', 'Risk-On', 'Calm markets with positive yield curve — conditions favour equities'
        else:
            return 'neutral', '→', 'Neutral', 'Mixed signals — stay selective and follow conviction scores'
    except Exception:
        return 'neutral', '→', 'Neutral', 'Market regime data unavailable'


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_holdings_enriched():
    rows = query("""
        SELECT h.ticker, h.shares, h.avg_buy_price, h.currency,
               u.sector, u.company_name
        FROM holdings h
        LEFT JOIN universe u ON h.ticker = u.ticker
        WHERE h.active = TRUE ORDER BY h.ticker
    """)
    result, total = [], 0.0
    fx = get_live_fx()
    for h in rows:
        pr = query("SELECT close FROM prices WHERE ticker=%s ORDER BY date DESC LIMIT 1",
                   (h['ticker'],))
        rate = fx.get(h['currency'] or 'EUR', fx['USD'])
        shares = float(h['shares'])
        avg = float(h['avg_buy_price'] or 0)
        cp = float(pr[0]['close']) if pr else avg
        cv = shares * cp * rate
        cost = shares * avg * rate
        pnl = cv - cost
        pnl_pct = (pnl / cost * 100) if cost else 0.0
        total += cv
        result.append(dict(
            ticker=h['ticker'], company=h['company_name'] or h['ticker'],
            shares=shares, avg_buy=avg, curr_price=cp,
            currency=h['currency'] or 'EUR', sector=h['sector'] or '—',
            curr_val=cv, pnl=pnl, pnl_pct=pnl_pct,
        ))
    return result, total


def _get_portfolio_history(holdings_rows, fx, days=90):
    """Return [{date, value}] for last N days using current holdings × historical prices."""
    import bisect
    if not holdings_rows:
        return []
    tickers = [h['ticker'] for h in holdings_rows]
    cutoff = date.today() - timedelta(days=days)
    rows = query("""
        SELECT ticker, date, close FROM prices
        WHERE ticker = ANY(%s) AND date >= %s ORDER BY date ASC
    """, (tickers, cutoff))
    if not rows:
        return []

    ticker_prices = {}
    for r in rows:
        ticker_prices.setdefault(r['ticker'], []).append((r['date'], float(r['close'])))

    h_info = {}
    for h in holdings_rows:
        ccy = h['currency'] or 'EUR'
        rate = fx.get(ccy, fx.get('USD', 0.853))
        h_info[h['ticker']] = (float(h['shares']), rate)

    all_dates = sorted(set(r['date'] for r in rows))
    result = []
    for d in all_dates:
        total = 0.0
        for t, (shares, rate) in h_info.items():
            tprices = ticker_prices.get(t, [])
            if not tprices:
                continue
            t_dates = [tp[0] for tp in tprices]
            idx = bisect.bisect_right(t_dates, d) - 1
            if idx >= 0:
                total += shares * tprices[idx][1] * rate
        result.append({'date': d.isoformat(), 'value': round(total, 2)})
    return result


def get_system_health():
    try:
        query("SELECT 1 AS x")
        db_ok = True
    except Exception:
        db_ok = False

    # Check Groq API reachability (fast)
    groq_ok = bool(os.environ.get('GROQ_API_KEY') or os.environ.get('GROQ'))
    or_ok   = bool(os.environ.get('OPENROUTER_API_KEY') or os.environ.get('OPENROUTER'))

    # LLM budget snapshot
    try:
        from analysis.rate_limit_manager import get_manager
        _rl = get_manager().status()
        od_remaining = _rl.get('ondemand_ticker_remaining', '?')
        groq_remaining = _rl.get('groq_tokens_remaining', '?')
    except Exception:
        od_remaining = '?'
        groq_remaining = '?'

    last = query("SELECT created_at FROM system_logs ORDER BY created_at DESC LIMIT 1")
    if last and last[0]['created_at']:
        diff = datetime.now() - last[0]['created_at']
        m = int(diff.total_seconds() / 60)
        last_str = (f"{m}m ago" if m < 60 else
                    f"{m//60}h ago" if m < 1440 else f"{m//1440}d ago")
    else:
        last_str = 'never'
    uni = query("SELECT COUNT(*) AS cnt FROM universe WHERE active=TRUE")
    uni_n = int(uni[0]['cnt']) if uni else 0
    now = datetime.now()
    nr = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now >= nr:
        nr += timedelta(days=1)
    while nr.weekday() >= 5:
        nr += timedelta(days=1)
    nm = int((nr - now).total_seconds() / 60)
    return dict(
        db_ok=db_ok, db='connected' if db_ok else 'error',
        groq_ok=groq_ok, or_ok=or_ok,
        od_remaining=od_remaining, groq_remaining=groq_remaining,
        last_run=last_str,
        next_run=f"in {nm}m" if nm < 60 else f"in {nm//60}h {nm%60}m",
        uni_count=uni_n,
    )


def get_pipeline_status():
    logs = query("""
        SELECT component, level, message, created_at FROM system_logs
        WHERE created_at >= NOW() - INTERVAL '25 hours'
        ORDER BY created_at ASC
    """)
    by_comp = {}
    for r in logs:
        by_comp.setdefault(r['component'], []).append(r)
    result = []
    now = datetime.now()
    for name, comps in STAGE_COMPONENTS:
        entries = []
        for c in comps:
            entries.extend(by_comp.get(c, []))
        if not entries:
            result.append(dict(name=name, badge='secondary', status='No data',
                               last_run='—', duration='—', last_msg='', age_hours=999))
            continue
        entries.sort(key=lambda r: r['created_at'])

        # Most recent run: entries within 10 min of the latest entry
        latest_ts = entries[-1]['created_at']
        run_entries = [e for e in entries
                       if (latest_ts - e['created_at']).total_seconds() <= 600]

        age_hours = (now - latest_ts).total_seconds() / 3600

        # Only flag ERROR/WARNING if this run had them (not stale old runs)
        levels = [e['level'] for e in run_entries]
        has_error = 'ERROR' in levels
        has_warn  = 'WARNING' in levels
        last_level = run_entries[-1]['level'] if run_entries else 'INFO'

        # Stale runs (>20h old) show as grey regardless of old errors
        if age_hours > 20:
            badge, status = 'secondary', 'Stale'
        elif has_error and last_level == 'ERROR':
            # Only show ERROR if the run actually ended in an error
            badge, status = 'danger', 'ERROR'
        elif has_error:
            # Had errors mid-run but completed successfully — show WARNING
            badge, status = 'warning', 'WARNING'
        elif has_warn:
            badge, status = 'warning', 'WARNING'
        else:
            badge, status = 'success', 'OK'

        if len(run_entries) > 1:
            dur = int((run_entries[-1]['created_at'] - run_entries[0]['created_at']).total_seconds())
            dur_str = f"{dur}s" if dur < 60 else f"{dur//60}m {dur%60}s"
        else:
            dur_str = '<1s'

        result.append(dict(
            name=name, badge=badge, status=status,
            last_run=latest_ts.strftime('%H:%M:%S'),
            duration=dur_str,
            last_msg=(run_entries[-1]['message'] or '')[:80],
            age_hours=round(age_hours, 1),
        ))
    return result


def get_cash():
    r = query("SELECT value FROM user_settings WHERE key='nordnet_cash_eur'")
    return float(r[0]['value']) if r else 0.0


# ── Alpha Vantage fundamentals — delegate to shared module ────────────────────
from pipeline.fundamentals_cache import (
    get_av_overview as _get_av_overview,
    build_yf_info as _build_yf_info,
    _safe_float,
)


# ── HTML layout ───────────────────────────────────────────────────────────────

_CSS = """
/* ── Design tokens ── */
:root{
  /* Backgrounds — dark, layered */
  --bg:#0b0f1a;--surface:#131929;--surface2:#1a2236;--border:#232f48;
  /* Text — WCAG AA contrast on --bg */
  --text:#e8edf5;        /* 12.5:1 on --bg  — primary readable text */
  --sub:#9aa5be;         /*  5.8:1 on --bg  — secondary/supporting */
  --muted:#7a8ba8;       /*  4.8:1 on --bg  — secondary labels; use --sub for longer text */
  /* Semantic colours — meaning only, not decoration */
  --green:#22c55e;--green-dim:rgba(34,197,94,.10);
  --red:#f43f5e;  --red-dim:rgba(244,63,94,.10);
  --amber:#f59e0b;--amber-dim:rgba(245,158,11,.10);
  --accent:#4f8ef7;      /* links, interactive */
}
/* Override Bootstrap CSS variables to match dark theme */
:root{
  --bs-body-bg:#0b0f1a;
  --bs-body-color:#e8edf5;
  --bs-table-color:#e8edf5;
  --bs-table-bg:transparent;
  --bs-table-striped-color:#e8edf5;
  --bs-table-hover-color:#e8edf5;
  --bs-border-color:#232f48;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
     font-family:'Inter',system-ui,sans-serif;font-size:.9rem;
     line-height:1.5;min-height:100vh}
/* ── Nav ── */
.adv-nav{background:var(--surface);border-bottom:1px solid var(--border);
  padding:.65rem 1.5rem;display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:100}
.adv-brand{font-weight:700;font-size:.95rem;color:var(--text);letter-spacing:-.3px;
           display:flex;align-items:center;gap:.5rem}
.adv-brand-dot{width:7px;height:7px;border-radius:50%;background:var(--accent);
               display:inline-block;flex-shrink:0}
.adv-nav-links{display:flex;gap:.1rem}
.adv-nav-links a{color:var(--sub);text-decoration:none;padding:.35rem .75rem;
  border-radius:6px;font-size:.82rem;font-weight:500;transition:background .12s,color .12s}
.adv-nav-links a:hover{color:var(--text);background:var(--surface2)}
.adv-nav-links a.active{color:var(--text);background:var(--surface2);font-weight:600}
/* ── Cards ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.card-header{background:var(--surface2);border-bottom:1px solid var(--border);
  padding:.6rem 1rem;font-weight:600;font-size:.78rem;color:var(--sub);
  letter-spacing:.4px;text-transform:uppercase}
.card-body{padding:.9rem 1rem}
.card-body.p-0{padding:0}
/* ── Tables ── */
.table{color:var(--text)!important;margin:0}
.table thead th{background:var(--surface2);border-color:var(--border);color:var(--sub);
  font-size:.73rem;font-weight:600;text-transform:uppercase;letter-spacing:.4px;padding:.5rem .85rem}
.table td,.table th{border-color:var(--border);vertical-align:middle;padding:.5rem .85rem}
.table tbody tr:hover{background:var(--surface2)}
/* ── Metric cards ── */
.metric-card{padding:1.1rem 1rem}
.metric-value{font-size:1.7rem;font-weight:700;color:var(--text);line-height:1.1;letter-spacing:-.5px}
.metric-label{color:var(--sub);font-size:.73rem;font-weight:500;margin-top:.3rem;
              text-transform:uppercase;letter-spacing:.5px}
/* ── Decision cards — the most important UI element ── */
.decision-card{border-radius:12px;padding:1.25rem 1.25rem 1rem;
  border:1px solid var(--border);margin-bottom:.75rem;position:relative}
.decision-card.buy {background:var(--green-dim);border-left:4px solid var(--green)}
.decision-card.sell{background:var(--red-dim);  border-left:4px solid var(--red)}
.decision-card.watch{background:var(--amber-dim);border-left:4px solid var(--amber)}
.decision-action{font-size:.72rem;font-weight:700;letter-spacing:.8px;text-transform:uppercase}
.decision-action.buy {color:var(--green)}
.decision-action.sell{color:var(--red)}
.decision-action.watch{color:var(--amber)}
.decision-ticker{font-size:1.35rem;font-weight:700;color:var(--text);line-height:1;letter-spacing:-.3px}
.decision-company{font-size:.83rem;color:var(--sub);margin-top:.1rem}
.decision-body{font-size:.88rem;color:var(--text);line-height:1.65;margin:.75rem 0}
.decision-detail{font-size:.8rem;color:var(--sub);font-weight:500}
.signal-pill{display:inline-flex;align-items:center;gap:.3rem;
  font-size:.72rem;padding:.2rem .55rem;border-radius:999px;font-weight:500;margin:.15rem .1rem}
.signal-pill.on {background:rgba(34,197,94,.12);color:var(--green);border:1px solid rgba(34,197,94,.25)}
.signal-pill.off{background:var(--surface2);color:var(--muted);border:1px solid var(--border)}
/* ── Conviction badge ── */
.conviction{font-size:.72rem;font-weight:600;padding:.2rem .6rem;border-radius:999px;
  border:1px solid;display:inline-block}
.conviction.high  {color:var(--green);border-color:rgba(34,197,94,.3);background:rgba(34,197,94,.08)}
.conviction.medium{color:var(--amber);border-color:rgba(245,158,11,.3);background:rgba(245,158,11,.08)}
.conviction.low   {color:var(--sub);border-color:var(--border);background:var(--surface2)}
/* ── Regime banner ── */
.regime-banner{border-radius:10px;padding:.75rem 1rem;margin-bottom:1rem;
  display:flex;align-items:center;gap:.75rem;font-size:.85rem;border:1px solid}
.regime-banner.bullish{background:rgba(34,197,94,.07);border-color:rgba(34,197,94,.2);color:var(--text)}
.regime-banner.neutral{background:rgba(79,142,247,.07);border-color:rgba(79,142,247,.2);color:var(--text)}
.regime-banner.bearish{background:rgba(245,158,11,.07);border-color:rgba(245,158,11,.2);color:var(--text)}
.regime-banner.risk-off{background:rgba(244,63,94,.07);border-color:rgba(244,63,94,.2);color:var(--text)}
/* ── Badges ── */
.badge-buy {background:var(--green);color:#000;font-weight:700}
.badge-sell{background:var(--red);  color:#fff;font-weight:700}
.badge-hold{background:var(--accent);color:#fff;font-weight:700}
.badge-watch{background:var(--amber);color:#000;font-weight:700}
/* ── Legacy ── */
.rc-buy {border-left:3px solid var(--green);background:var(--green-dim)}
.rc-sell{border-left:3px solid var(--red);  background:var(--red-dim)}
.rc-watch{border-left:3px solid var(--amber);background:var(--amber-dim)}
.rc-hold{border-left:3px solid var(--accent);background:rgba(79,142,247,.07)}
/* ── Tabs ── */
.nav-tabs{border-color:var(--border)}
.nav-tabs .nav-link{color:var(--sub);border-color:transparent;font-size:.83rem;padding:.45rem .9rem}
.nav-tabs .nav-link.active{color:var(--text);background:var(--surface);
  border-color:var(--border) var(--border) var(--surface);font-weight:600}
.nav-tabs .nav-link:hover{color:var(--text)}
/* ── Forms ── */
.form-control,.form-select{background:var(--surface2)!important;color:var(--text)!important;
  border-color:var(--border)!important;border-radius:7px}
.form-control:focus,.form-select:focus{background:var(--surface2)!important;
  color:var(--text)!important;box-shadow:0 0 0 2px rgba(79,142,247,.25)!important;
  border-color:var(--accent)!important}
.form-label{color:var(--sub);font-size:.8rem;font-weight:500;margin-bottom:.3rem}
.btn-primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn-success{background:#16a34a;border-color:#16a34a;color:#fff}
.btn-outline-success{color:var(--green);border-color:var(--green)}
.btn-outline-success:hover{background:var(--green);color:#000}
.btn-outline-danger{color:var(--red);border-color:var(--red)}
.btn-outline-danger:hover{background:var(--red);color:#fff}
/* ── Modal ── */
.modal-content{background:var(--surface);color:var(--text);
  border:1px solid var(--border);border-radius:12px}
.modal-header,.modal-footer{border-color:var(--border)}
.modal-header{background:var(--surface2);padding:.75rem 1rem}
/* ── Progress / misc ── */
.progress{background:var(--surface2);border-radius:99px}
.bg-secondary{background:#253047!important}
.bg-success{background:var(--green)!important}
.bg-danger {background:var(--red)!important}
.bg-warning{background:var(--amber)!important}
a{color:var(--accent)}
.text-success{color:var(--green)!important}
.text-danger {color:var(--red)!important}
.text-warning{color:var(--amber)!important}
.text-muted  {color:var(--sub)!important}
.alert-success{background:var(--green-dim);border-color:var(--green);color:var(--green)}
.alert-info   {background:rgba(79,142,247,.08);border-color:rgba(79,142,247,.3);color:var(--text)}
.alert-warning{background:var(--amber-dim);border-color:var(--amber);color:var(--text)}
/* ── Footer ── */
.adv-footer{background:var(--surface);border-top:1px solid var(--border);
  padding:.5rem 1.5rem;font-size:.73rem;color:var(--sub);
  display:flex;flex-wrap:wrap;gap:1rem;align-items:center}
.adv-footer .dot{width:6px;height:6px;border-radius:50%;display:inline-block;
  margin-right:.3rem;vertical-align:middle}
.page-header{font-size:.95rem;font-weight:600;color:var(--text);margin-bottom:1rem}
"""

# Jinja2 template strings — NO f-strings here
_NAV_TMPL = (
    '{% set _p = current_page %}'
    '<nav class="adv-nav">'
    '<div class="adv-brand"><span class="adv-brand-dot"></span>Advisor</div>'
    '<div class="adv-nav-links">'
    '<a class="{{ \'active\' if _p==\'Today\' else \'\' }}" href="/">Today</a>'
    '<a class="{{ \'active\' if _p==\'Portfolio\' else \'\' }}" href="/portfolio">Portfolio</a>'
    '<a class="{{ \'active\' if _p==\'Research\' else \'\' }}" href="/research">Research</a>'
    '<a class="{{ \'active\' if _p==\'Watchlist\' else \'\' }}" href="/watchlist">Watchlist</a>'
    '<a class="{{ \'active\' if _p==\'Allocation\' else \'\' }}" href="/exposure">Allocation</a>'
    '<a class="{{ \'active\' if _p==\'Pipeline\' else \'\' }}" href="/pipeline">Pipeline</a>'
    '<a class="{{ \'active\' if _p==\'History\' else \'\' }}" href="/history">History</a>'
    '<a class="{{ \'active\' if _p==\'Settings\' else \'\' }}" href="/settings">Settings</a>'
    '</div></nav>'
)

_RESEARCH_SUBNAV = (
    '{% set _rp = research_page|default(\'\') %}'
    '<div style="display:flex;gap:.25rem;margin-bottom:1.25rem;flex-wrap:wrap">'
    '<a href="/data"     class="btn btn-sm {{ \'btn-primary\' if _rp==\'data\'     else \'btn-outline-secondary\' }}" style="font-size:.8rem">Data Health</a>'
    '<a href="/signals"  class="btn btn-sm {{ \'btn-primary\' if _rp==\'signals\'  else \'btn-outline-secondary\' }}" style="font-size:.8rem">Signals</a>'
    '<a href="/universe" class="btn btn-sm {{ \'btn-primary\' if _rp==\'universe\' else \'btn-outline-secondary\' }}" style="font-size:.8rem">Universe</a>'
    '<a href="/accuracy" class="btn btn-sm {{ \'btn-primary\' if _rp==\'accuracy\' else \'btn-outline-secondary\' }}" style="font-size:.8rem">Accuracy</a>'
    '</div>'
)

_HEALTH_TMPL = (
    '</div>'  # closes page wrapper
    '<footer class="adv-footer">'
    '<span><span class="dot" style="background:{{ \'#22c55e\' if health.db_ok else \'#f43f5e\' }}"></span>'
    'DB {{ health.db }}</span>'
    '<span id="llmStatusFooter"><span class="dot" style="background:{{ \'#22c55e\' if health.groq_ok else \'#6b7a99\' }}"></span>'
    'Groq+OR</span>'
    '<span id="footerBudget" style="cursor:pointer" title="On-demand remaining today">'
    'On-demand: {{ health.od_remaining }} left</span>'
    '<span>Last run: {{ health.last_run }}</span>'
    '<span>Next run: {{ health.next_run }}</span>'
    '<span>{{ health.uni_count }} stocks in universe</span>'
    '</footer>'
    '<div id="analysisRunningBanner" style="display:none;position:fixed;bottom:42px;left:0;right:0;'
    'background:rgba(245,158,11,.92);color:#1a1f2e;font-size:.78rem;font-weight:600;'
    'text-align:center;padding:.35rem;z-index:9999;letter-spacing:.01em">'
    '&#9889; AI analysis running (<span id="analysisBusyTicker"></span>) — Groq + OpenRouter agents working'
    '</div>'
    '<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>'
    '<script>'
    '(function pollAnalysis(){'
    '  fetch("/api/analysis-status").then(r=>r.json()).then(function(d){'
    '    var banner=document.getElementById("analysisRunningBanner");'
    '    var tickerSpan=document.getElementById("analysisBusyTicker");'
    '    var footerSpan=document.getElementById("llmStatusFooter");'
    '    var busy=d.busy;'
    '    var label=d.active_ticker||(d.pipeline_busy?"pipeline":"");'
    '    if(banner){banner.style.display=busy?"block":"none";}'
    '    if(tickerSpan){tickerSpan.textContent=label||"...";}'
    '    if(footerSpan){var dot=footerSpan.querySelector(".dot");'
    '      if(dot){dot.style.background=busy?"#f59e0b":"#22c55e";}'
    '    }'
    '    document.querySelectorAll(".analyze-disabled-when-busy").forEach(function(el){'
    '      el.disabled=busy;'
    '      if(busy){el.title="AI analysis running — wait for current ticker to finish";}'
    '    });'
    '  }).catch(function(){});'
    '  setTimeout(pollAnalysis,8000);'
    '})();'
    '</script>'
    '</body></html>'
)

_FLASH_TMPL = (
    '{% for _m in get_flashed_messages(with_categories=true) %}'
    '<div class="alert alert-{{ _m[0] if _m[0] in (\'success\',\'warning\',\'danger\',\'info\') else \'success\' }}'
    ' alert-dismissible fade show py-2 mb-3" '
    'style="color:#e8edf5!important;font-size:.85rem">'
    '{{ _m[1] }}'
    '<button type="button" class="btn-close btn-close-white" data-bs-dismiss="alert"></button></div>'
    '{% endfor %}'
)


def make_page(content, page_name, refresh=None):
    refresh_tag = (
        '<meta http-equiv="refresh" content="' + str(refresh) + '">'
        if refresh else ''
    )
    head = (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        + refresh_tag +
        '<title>Advisor — ' + page_name + '</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">'
        '<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">'
        '<style>' + _CSS + '</style>'
        '</head><body>'
    )
    body_open = '<div class="container-fluid px-3 py-3">' + _FLASH_TMPL
    return head + _NAV_TMPL + body_open + content + _HEALTH_TMPL


# ── Page templates (Jinja2) ───────────────────────────────────────────────────

_INDEX = """
{# ── Pipeline running banner ── #}
{% if pipeline_running %}
<div class="alert alert-info d-flex align-items-center gap-2 py-2 mb-3" style="font-size:.85rem">
  <span class="spinner-border spinner-border-sm" role="status"></span>
  <strong>Pipeline is running</strong> — <a href="/pipeline" style="color:inherit">view live progress</a>
</div>
{% endif %}

{# ── Market regime banner ── #}
<div class="regime-banner {{ regime_class }} mb-3">
  <span style="font-size:1.25rem;line-height:1">{{ regime_icon }}</span>
  <div>
    <strong style="font-size:.9rem">Market Regime: {{ regime_label }}</strong>
    <div style="font-size:.8rem;color:var(--sub);margin-top:.1rem">{{ regime_desc }}</div>
  </div>
  <div class="ms-auto d-flex align-items-center gap-3" style="font-size:.78rem;color:var(--sub)">
    <span>&#8364;{{ (total_value + cash)|int }} total</span>
    <button type="button" class="btn btn-sm btn-outline-success"
            style="font-size:.72rem;padding:.2rem .65rem"
            data-bs-toggle="modal" data-bs-target="#cashModal"
            title="Deposit cash">&#8364;{{ cash|int }} cash &#43;</button>
    <span>{{ holdings|length }} positions</span>
    {% if pipeline_running %}
    <a href="/pipeline" class="btn btn-sm btn-outline-warning d-flex align-items-center gap-1" style="font-size:.72rem;padding:.2rem .65rem">
      <span class="spinner-border spinner-border-sm" style="width:.6rem;height:.6rem;border-width:2px"></span>Running
    </a>
    {% else %}
    <form method="POST" action="/run-pipeline" style="display:inline"
          onsubmit="var b=this.querySelector('button');b.textContent='Starting…';b.disabled=true;">
      <button type="submit" class="btn btn-sm btn-outline-secondary" style="font-size:.72rem;padding:.2rem .65rem">
        ▶ Run Pipeline
      </button>
    </form>
    {% endif %}
  </div>
</div>

{# ── Today's decisions — the main content ── #}
<div class="d-flex justify-content-between align-items-center mb-2">
  <span style="font-size:.78rem;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.5px">
    Today's Decisions &mdash; {{ today }}
  </span>
  <button class="btn btn-sm btn-success px-3" data-bs-toggle="modal" data-bs-target="#tradeModal"
          style="font-size:.78rem">+ Log Trade</button>
</div>

{% set pending_recs = recs | selectattr('acted_on', 'equalto', false) | list %}
{% set done_recs    = recs | selectattr('acted_on', 'equalto', true)  | list %}

{# ── Executed today ── #}
{% if done_recs %}
<div class="mb-3">
  <div style="font-size:.72rem;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.5px;margin-bottom:.5rem">
    ✓ Actions taken today
  </div>
  {% for r in done_recs %}
  {% set rp = rec_prices.get(r.ticker, 0) %}
  {% set rshares = rec_shares_map.get(r.ticker, 0) %}
  {% set rccy = rec_currency_map.get(r.ticker, 'USD') %}
  <div class="decision-card {{ r.action|lower }}" style="opacity:.65;border-left-color:var(--sub)">
    <div class="d-flex justify-content-between align-items-center">
      <div>
        <span class="decision-action {{ r.action|lower }}" style="color:var(--sub)">{{ r.action }}</span>
        <a href="/ticker/{{ r.ticker }}" class="decision-ticker ms-2 text-decoration-none"
           style="color:var(--text)">{{ r.ticker }}</a>
        {% if r.company_name and r.company_name != r.ticker %}
        <span class="decision-company"> — {{ r.company_name }}</span>
        {% endif %}
        <span class="badge bg-secondary ms-2" style="font-size:.65rem;vertical-align:middle">Executed</span>
      </div>
      <div class="d-flex gap-2 align-items-center">
        {% if rp > 0 %}<span style="font-size:.82rem;color:var(--sub)">{{ rccy }} {{ rp }}</span>{% endif %}
        <a href="/ticker/{{ r.ticker }}" class="btn btn-sm btn-outline-secondary"
           style="font-size:.72rem;padding:.2rem .55rem">View</a>
        {% if r.action in ('BUY','SELL') %}
        <button class="btn btn-sm btn-outline-warning"
                style="font-size:.72rem;padding:.2rem .55rem"
                onclick="quickTrade('{{ r.ticker }}','{{ r.action }}',{{ [rshares|int,1]|max }},{{ rp }},'{{ rccy }}',this)">
          Repeat
        </button>
        {% endif %}
      </div>
    </div>
    <div class="collapse mt-2" id="done-detail-{{ r.ticker }}">
      <div style="font-size:.78rem;color:var(--sub)">{{ r.natural_summary }}</div>
    </div>
  </div>
  {% endfor %}
</div>
{% endif %}

{# ── Pending suggestions ── #}
{% if pending_recs %}
{% if done_recs %}
<div style="font-size:.72rem;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.5px;margin-bottom:.5rem">
  Pending suggestions
</div>
{% endif %}
{% for r in pending_recs %}
{% set rp = rec_prices.get(r.ticker, 0) %}
{% set rshares = rec_shares_map.get(r.ticker, 0) %}
{% set rccy = rec_currency_map.get(r.ticker, 'USD') %}
{% set conf_pct = (r.confidence|float * 100)|int %}
{% set conv_class = 'high' if r.confidence|float >= 0.75 else ('medium' if r.confidence|float >= 0.5 else 'low') %}
{% set conv_label = 'High conviction' if r.confidence|float >= 0.75 else ('Medium conviction' if r.confidence|float >= 0.5 else 'Low conviction') %}
<div class="decision-card {{ r.action|lower }}" id="card-{{ r.ticker }}">
  {# Header row: action + ticker + conviction #}
  <div class="d-flex justify-content-between align-items-start mb-2">
    <div>
      <span class="decision-action {{ r.action|lower }}">{{ r.action }}</span>
      <a href="/ticker/{{ r.ticker }}" class="decision-ticker ms-2 text-decoration-none"
         style="color:var(--text)">{{ r.ticker }}</a>
      {% if r.company_name and r.company_name != r.ticker %}
      <span class="decision-company"> — {{ r.company_name }}</span>
      {% endif %}
    </div>
    <div class="d-flex align-items-center gap-2">
      <span class="conviction {{ conv_class }}">{{ conv_label }}</span>
      {% if rp > 0 %}
      <span style="font-size:.82rem;color:var(--sub)">{{ rccy }} {{ rp }}</span>
      {% endif %}
    </div>
  </div>

  {# Natural language summary — the "why" in plain English #}
  <p class="decision-body mb-2">{{ r.natural_summary }}</p>

  {# Signal pills — quick visual of what triggered this #}
  {% if r.signal_sources %}
  <div class="mb-2">
    {% for src in r.signal_sources.split(',') if src.strip() %}
    <span class="signal-pill on">{{ src.strip() }}</span>
    {% endfor %}
  </div>
  {% endif %}

  {# Action footer — execute button + expand for detail #}
  <div class="d-flex align-items-center justify-content-between" style="border-top:1px solid rgba(255,255,255,.06);padding-top:.6rem;margin-top:.2rem">
    <div class="d-flex gap-2" id="footer-{{ r.ticker }}">
      {% if r.action in ('BUY', 'SELL') %}
        {% if r.action == 'SELL' and r.ticker not in holdings_tickers %}
        <span style="font-size:.73rem;color:var(--sub)">Not in holdings</span>
        {% else %}
        <button class="btn btn-sm {{ 'btn-success' if r.action=='BUY' else 'btn-danger' }}"
                style="font-size:.75rem;padding:.25rem .75rem"
                id="execbtn-{{ r.ticker }}"
                onclick="quickTrade('{{ r.ticker }}','{{ r.action }}',{{ [rshares|int,1]|max }},{{ rp }},'{{ rccy }}',this)">
          Execute {{ r.action }}{% if rshares > 0 %} &mdash; {{ [rshares|int, 1]|max }} shares{% endif %}
        </button>
        {% endif %}
      {% endif %}
      <a href="/ticker/{{ r.ticker }}" class="btn btn-sm btn-outline-secondary"
         style="font-size:.75rem;padding:.25rem .65rem">View Analysis</a>
    </div>
    <button class="btn btn-sm" style="font-size:.72rem;color:var(--sub);padding:.25rem .5rem;background:none;border:none"
            data-bs-toggle="collapse" data-bs-target="#detail-{{ r.ticker }}">
      Show reasoning &#8964;
    </button>
  </div>

  {# Collapsed detail — full reasoning for those who want it #}
  <div id="detail-{{ r.ticker }}" class="collapse mt-2" style="border-top:1px solid rgba(255,255,255,.06);padding-top:.6rem">
    <div style="font-size:.8rem;color:var(--sub);line-height:1.7">{{ r.reasoning or '—' }}</div>
    {% if r.bull_case is defined and r.bull_case %}
    <div style="margin-top:.5rem;font-size:.75rem;color:var(--green)"><strong>Bull:</strong> {{ r.bull_case }}</div>
    {% endif %}
    {% if r.bear_case is defined and r.bear_case %}
    <div style="margin-top:.25rem;font-size:.75rem;color:var(--red)"><strong>Bear:</strong> {{ r.bear_case }}</div>
    {% endif %}
  </div>
</div>
{% endfor %}
{% elif not done_recs %}
{# Empty state — no recs at all #}
<div class="card mb-3">
  <div class="card-body text-center py-5">
    <div style="font-size:2.5rem;margin-bottom:.75rem;opacity:.4">📋</div>
    <div style="color:var(--sub);font-size:.9rem;margin-bottom:.4rem">No decisions yet for today</div>
    <div style="color:var(--sub);font-size:.8rem;margin-bottom:1rem">
      Pipeline runs at 04:00 on weekdays.
      {% if pipeline_running %}Running now — check back in a few minutes.{% endif %}
    </div>
    {% if not pipeline_running %}
    <form method="POST" action="/run-pipeline"
          onsubmit="var b=this.querySelector('button');b.textContent='⏳ Starting…';b.disabled=true;">
      <button type="submit" class="btn btn-sm btn-outline-success" style="font-size:.82rem">
        ▶ Run Pipeline Now
      </button>
    </form>
    {% else %}
    <span class="d-flex align-items-center gap-2" style="font-size:.82rem;color:var(--amber)">
      <span class="spinner-border spinner-border-sm"></span> Pipeline is running…
      <a href="/pipeline" style="color:var(--accent)">View progress</a>
    </span>
    {% endif %}
  </div>
</div>
{% endif %}

{# ── Holdings — compact overview ── #}
{% if holdings %}
<div class="card mt-3">
  <div class="card-header d-flex justify-content-between align-items-center">
    <span>Portfolio &mdash; {{ holdings|length }} positions, &#8364;{{ total_value|int }} invested</span>
    <a href="/history" style="font-size:.75rem;color:var(--accent)">Full history &#8250;</a>
  </div>
  <div class="card-body p-0">
    <table class="table table-sm mb-0">
      <thead><tr>
        <th>Ticker</th><th>Company</th><th>Value</th><th>P&amp;L</th>
        <th style="color:var(--sub);font-size:.7rem">Signal today</th>
      </tr></thead>
      <tbody>
      {% for h in holdings %}
      {% set h_rec = holdings_rec_map.get(h.ticker) %}
      <tr>
        <td><a href="/ticker/{{ h.ticker }}" style="font-weight:700;color:var(--accent);text-decoration:none">{{ h.ticker }}</a></td>
        <td style="color:var(--sub);font-size:.82rem">{{ h.company }}</td>
        <td><strong>&#8364;{{ h.curr_val|int }}</strong></td>
        <td>
          {% if h.pnl >= 0 %}
          <span class="text-success" style="font-size:.85rem">+{{ "%.1f"|format(h.pnl_pct) }}%</span>
          {% else %}
          <span class="text-danger" style="font-size:.85rem">{{ "%.1f"|format(h.pnl_pct) }}%</span>
          {% endif %}
        </td>
        <td>
          {% if h_rec %}
          <span class="badge badge-{{ h_rec.action|lower }}" style="font-size:.67rem">{{ h_rec.action }}</span>
          {% else %}
          <span style="color:var(--muted);font-size:.75rem">—</span>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endif %}

{# ── Sector + Regional exposure ── #}
{% if sector_targets %}
<div class="card mt-3">
  <div class="card-header d-flex justify-content-between align-items-center">
    <span>Sector Allocation</span>
    <a href="/exposure" style="font-size:.75rem;color:var(--accent)">Edit targets &#8250;</a>
  </div>
  <div class="card-body py-2 px-3">
    <div class="row g-1">
    {% for sector, tgt in sector_targets.items() if tgt > 0 %}
    {% set cur = sector_exposure.get(sector, 0) %}
    {% set gap = cur - tgt %}
    <div class="col-6 col-md-3 col-lg-2">
      <div style="font-size:.65rem;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="{{ sector }}">{{ sector }}</div>
      <div class="progress" style="height:6px;background:var(--surface2);border-radius:3px">
        <div class="progress-bar" style="width:{{ [cur/tgt*100 if tgt else 0, 100]|min|int }}%;
          background:{% if gap > 7 %}var(--red){% elif gap < -7 %}var(--amber){% else %}var(--green){% endif %};
          border-radius:3px"></div>
      </div>
      <div style="font-size:.62rem;color:var(--muted)">{{ '%.0f'|format(cur) }}% / {{ tgt }}%</div>
    </div>
    {% endfor %}
    </div>
  </div>
</div>
{% endif %}

{# ── Geographic / regional exposure ── #}
{% if region_exposure %}
<div class="card mt-2">
  <div class="card-header d-flex justify-content-between align-items-center">
    <span>Geographic Exposure</span>
    <small style="color:var(--sub);font-size:.72rem">by region</small>
  </div>
  <div class="card-body py-2 px-3">
    {% set region_colors = {'Americas': '#60a5fa', 'Europe': '#34d399', 'Asia-Pacific': '#f59e0b', 'Other': '#a78bfa'} %}
    <div class="d-flex flex-wrap gap-3 align-items-center">
    {% for region, pct in region_exposure.items() %}
    {% set col = region_colors.get(region, '#888') %}
    <div style="min-width:90px">
      <div style="font-size:.65rem;color:var(--sub);margin-bottom:.2rem">{{ region }}</div>
      <div class="progress" style="height:8px;background:var(--surface2);border-radius:4px">
        <div class="progress-bar" style="width:{{ pct|int }}%;background:{{ col }};border-radius:4px"></div>
      </div>
      <div style="font-size:.68rem;color:var(--text);font-weight:600;margin-top:.15rem">{{ '%.0f'|format(pct) }}%</div>
    </div>
    {% endfor %}
    {% set us_heavy = region_exposure.get('Americas', 0) > 70 %}
    {% if us_heavy %}
    <div style="font-size:.7rem;color:var(--amber);padding:.3rem .6rem;background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.2);border-radius:6px">
      &#9888; US-heavy portfolio — consider diversifying into Europe/Asia
    </div>
    {% endif %}
    </div>
  </div>
</div>
{% endif %}

{# ── Deposit Cash modal ── #}
<div class="modal fade" id="cashModal" tabindex="-1">
  <div class="modal-dialog modal-sm">
    <div class="modal-content">
      <div class="modal-header">
        <h6 class="modal-title">&#128176; Deposit Cash</h6>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <form method="POST" action="/add-cash">
        <div class="modal-body">
          <div style="font-size:.78rem;color:var(--muted);margin-bottom:.75rem">
            Current balance: <strong style="color:var(--green)">€{{ cash|int }}</strong>
          </div>
          <label class="form-label" style="font-size:.82rem">Amount to deposit (EUR)</label>
          <input type="number" name="amount" class="form-control" step="0.01"
                 placeholder="e.g. 500" min="0.01" required autofocus>
          <div class="form-text">Added on top of current balance.</div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="submit" class="btn btn-sm btn-success">&#43; Deposit</button>
        </div>
      </form>
    </div>
  </div>
</div>

{# ── Trade modal ── #}
<div class="modal fade" id="tradeModal" tabindex="-1">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h6 class="modal-title fw-semibold" id="portfolioTradeTitle">Log Completed Trade</h6>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <form method="POST" action="/trade">
        <div class="modal-body">
          <div class="row g-3">
            <div class="col-6">
              <label class="form-label">Ticker</label>
              <input name="ticker" class="form-control" placeholder="AAPL" required>
            </div>
            <div class="col-6">
              <label class="form-label">Action</label>
              <select name="action" class="form-select"><option>BUY</option><option>SELL</option></select>
            </div>
            <div class="col-6">
              <label class="form-label">Shares</label>
              <input name="shares" type="number" step="0.0001" class="form-control" required>
            </div>
            <div class="col-6">
              <label class="form-label">Price</label>
              <input name="price" type="number" step="0.0001" class="form-control" required>
            </div>
            <div class="col-6">
              <label class="form-label">Currency</label>
              <input name="currency" class="form-control" value="EUR">
            </div>
            <div class="col-6">
              <label class="form-label">Date</label>
              <input name="trade_date" type="date" class="form-control" value="{{ today }}">
            </div>
            <div class="col-12">
              <label class="form-label">Notes</label>
              <input name="notes" class="form-control" placeholder="Optional">
            </div>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Cancel</button>
          <button type="submit" class="btn btn-success btn-sm px-4" id="portfolioTradeBtn">Log Trade</button>
        </div>
      </form>
    </div>
  </div>
</div>
<script>
function quickTrade(ticker, action, shares, price, currency, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Logging…'; }
  var fd = new FormData();
  fd.append('ticker', ticker);
  fd.append('action', action);
  fd.append('shares', Math.max(1, Math.round(shares)));
  fd.append('price', price || 0);
  fd.append('currency', currency || 'USD');
  fetch('/trade', {method:'POST', body:fd, headers:{'X-Requested-With':'XMLHttpRequest'}})
    .then(function(r){ return r.text(); })
    .then(function(html) {
      // Mark card as executed
      var card = document.getElementById('card-' + ticker);
      if (card) {
        var footer = document.getElementById('footer-' + ticker);
        if (footer) footer.innerHTML =
          '<span class="badge bg-success" style="font-size:.72rem">✓ Executed</span>' +
          '<a href="/ticker/'+ticker+'" class="btn btn-sm btn-outline-secondary ms-2" style="font-size:.72rem;padding:.2rem .55rem">View</a>';
        card.style.opacity = '0.65';
        card.style.borderLeftColor = 'var(--sub)';
      }
      // Show a quick toast
      var t = document.createElement('div');
      t.className = 'alert alert-success alert-dismissible fade show py-2 mb-3';
      t.style = 'position:fixed;top:70px;right:1rem;z-index:9999;min-width:220px;font-size:.83rem';
      t.innerHTML = '✓ ' + action + ' ' + ticker + ' logged' +
        '<button type="button" class="btn-close btn-close-white" data-bs-dismiss="alert"></button>';
      document.body.appendChild(t);
      setTimeout(function(){ t.remove(); }, 4000);
    })
    .catch(function(e) {
      if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
      alert('Error: ' + e);
    });
}

function executeSignal(ticker, action, shares, price, currency) {
  var f = document.querySelector('#tradeModal form');
  if (!f) return;
  f.querySelector('[name="ticker"]').value = ticker || '';
  var sel = f.querySelector('[name="action"]');
  if (sel) { for (var i=0;i<sel.options.length;i++) if (sel.options[i].value===action) sel.selectedIndex=i; }
  f.querySelector('[name="shares"]').value = shares ? Math.max(1, Math.round(shares)) : '';
  f.querySelector('[name="price"]').value = price || '';
  f.querySelector('[name="currency"]').value = currency || 'EUR';
  var btn = document.getElementById('portfolioTradeBtn');
  var title = document.getElementById('portfolioTradeTitle');
  if (btn) {
    btn.className = action === 'SELL' ? 'btn btn-danger btn-sm px-4' : 'btn btn-success btn-sm px-4';
    btn.textContent = 'Execute ' + action;
  }
  if (title) title.textContent = action + ' ' + ticker;
}
</script>
"""

_PIPELINE = """
<div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
  <span class="page-header mb-0">Pipeline Status</span>
  <div class="d-flex align-items-center gap-2">
    <small style="color:var(--muted);font-size:.75rem" id="localTime"></small>
    <form method="POST" action="/reload-universe" style="display:inline">
      <button class="btn btn-sm btn-outline-secondary px-3"
              onclick="return confirm('Reload full universe? This takes several minutes.')">
        &#8635; Reload Universe
      </button>
    </form>
    <form method="POST" action="/run-pipeline" id="pipelineForm" style="display:inline"
          onsubmit="var b=document.getElementById('runPipelineBtn');b.textContent='⏳ Starting…';b.disabled=true;">
      <button type="submit" class="btn btn-sm btn-outline-success px-3" id="runPipelineBtn">
        &#9654; Run Pipeline Now
      </button>
    </form>
  </div>
</div>

{# ── Live pipeline stage tracker ─────────────────────────────────────────── #}
<div class="card mb-4" id="pipelineTracker">
  <div class="card-header d-flex align-items-center gap-2">
    <span id="pipelineStatusLabel">Idle</span>
    <span class="spinner-border spinner-border-sm d-none" id="pipelineSpinner" role="status"></span>
    <span class="ms-auto" style="font-size:.72rem;color:var(--muted)" id="pipelineTs"></span>
  </div>
  <div class="card-body py-3 px-3">
    <div id="pipelineStages" class="d-flex flex-wrap gap-2">
      <!-- populated by JS -->
    </div>
  </div>
</div>

<script>
(function() {
  var STAGES = [
    {key:'collect_all',        label:'Data Collection'},
    {key:'event_classifier',   label:'Event Classifier'},
    {key:'event_mapper',       label:'Event Mapper'},
    {key:'scorer',             label:'Candidate Scorer'},
    {key:'tax_filter',         label:'Tax Filter'},
    {key:'trading_agents',     label:'TradingAgents'},
    {key:'holdings_monitor',   label:'Holdings Monitor'},
    {key:'portfolio_brain',    label:'Portfolio Brain'},
    {key:'sanity_check',       label:'Sanity Check'},
    {key:'performance_tracker',label:'Performance'},
  ];
  var stagesCompleted = new Set();
  var stageActive = null;

  function renderStages(current) {
    var container = document.getElementById('pipelineStages');
    if (!container) return;
    var html = '';
    var activeIdx = STAGES.findIndex(s => s.key === current);
    STAGES.forEach(function(s, i) {
      var isDone    = stagesCompleted.has(s.key) && s.key !== current;
      var isRunning = (s.key === current);
      var isPending = !isDone && !isRunning;
      var bg, col, border, icon;
      if (isRunning) {
        bg = 'rgba(59,130,246,.15)'; col = '#60a5fa'; border = '#3b82f6'; icon = '⏳';
      } else if (isDone) {
        bg = 'rgba(34,197,94,.12)'; col = 'var(--green)'; border = 'var(--green)'; icon = '✓';
      } else {
        bg = 'var(--surface2)'; col = 'var(--muted)'; border = 'var(--border)'; icon = '○';
      }
      var labelText = s.label;
      // Append TA progress to the TradingAgents stage pill
      if (isRunning && s.key === 'trading_agents' && window._taProg) {
        var p = window._taProg;
        labelText += ' (' + p.completed + '/' + p.expected + ')';
        if (p.in_progress) labelText += ' — ' + p.in_progress;
      }
      html += '<div style="display:flex;align-items:center;gap:.35rem;padding:.3rem .65rem;border-radius:999px;' +
              'border:1px solid ' + border + ';background:' + bg + ';font-size:.75rem;color:' + col + ';' +
              'font-weight:' + (isRunning ? '700' : '500') + '">' +
              (isRunning ? '<span class="spinner-border" style="width:.7rem;height:.7rem;border-width:2px"></span>' : '<span>' + icon + '</span>') +
              '<span>' + labelText + '</span></div>';
    });

    // Show overall TA progress bar if active
    var prog = window._taProg;
    if (prog && prog.expected > 0) {
      var pct = Math.min(100, Math.round(prog.completed / prog.expected * 100));
      html += '<div style="flex-basis:100%;margin-top:.5rem">' +
              '<div style="font-size:.7rem;color:var(--sub);margin-bottom:.25rem">AI Analysis: ' + prog.completed + '/' + prog.expected + ' tickers (' + pct + '%)</div>' +
              '<div class="progress" style="height:5px;background:var(--surface2);border-radius:3px">' +
              '<div class="progress-bar" style="width:' + pct + '%;background:var(--accent);border-radius:3px;transition:width .5s"></div>' +
              '</div></div>';
    }

    container.innerHTML = html;
  }

  function pollPipeline() {
    fetch('/api/pipeline-stage')
      .then(r => r.json())
      .then(function(data) {
        var c = data.current || {};
        var running = data.running;
        var stage   = c.stage || 'idle';
        var status  = c.status || 'idle';

        // Store TA progress for renderStages
        window._taProg = data.ta_progress || null;

        // Track completed stages within this run
        if (status === 'done' && stage !== 'idle') stagesCompleted.add(stage);
        if (stage === 'idle' && !running) stagesCompleted.clear();

        // Update header
        var lbl = document.getElementById('pipelineStatusLabel');
        var spin = document.getElementById('pipelineSpinner');
        var ts = document.getElementById('pipelineTs');
        var btn = document.getElementById('runPipelineBtn');

        if (running) {
          var taNote = (stage === 'trading_agents' && data.ta_progress)
            ? ' [' + data.ta_progress.completed + '/' + data.ta_progress.expected + ' tickers]' : '';
          if (lbl) lbl.textContent = '▶ Running: ' + (c.label || stage) + taNote;
          if (spin) spin.classList.remove('d-none');
          if (btn) { btn.disabled = true; btn.textContent = '⏳ Running…'; }
        } else {
          if (lbl) lbl.textContent = status === 'crashed' ? '⚠ Crashed at: ' + (c.label||stage) : 'Idle — last run complete';
          if (spin) spin.classList.add('d-none');
          if (btn) { btn.disabled = false; btn.textContent = '▶ Run Pipeline Now'; }
        }
        if (ts && c.ts) ts.textContent = c.ts.replace('T',' ').replace('Z','') + ' UTC';

        renderStages(running ? stage : null);
        setTimeout(pollPipeline, running ? 3000 : 15000);
      })
      .catch(function() { setTimeout(pollPipeline, 10000); });
  }

  function updateTime() {
    var el = document.getElementById('localTime');
    if (el) el.textContent = new Date().toLocaleTimeString();
  }
  updateTime(); setInterval(updateTime, 1000);
  pollPipeline();
})();
</script>

<div class="row g-3 mb-4">
  {# ── Today's recommendations (full TradingAgents output) ── #}
  <div class="col-md-7">
    <div class="card h-100">
      <div class="card-header">Today&#39;s Recommendations — Full Detail</div>
      <div class="card-body" style="padding:.75rem">
        {% if recs %}
        {% for r in recs %}
        <div class="rc-{{ r.action|lower }} rounded-3 p-3 mb-2" style="border:1px solid var(--border)">
          <div class="d-flex justify-content-between align-items-start mb-1">
            <div>
              <a href="/ticker/{{ r.ticker }}" style="font-weight:700;font-size:1rem;color:var(--accent);text-decoration:none">{{ r.ticker }}</a>
              <span class="badge badge-{{ r.action|lower }} ms-2" style="font-size:.7rem">{{ r.action }}</span>
              {% if r.company_name and r.company_name != r.ticker %}
              <span style="color:var(--muted);font-size:.78rem;margin-left:.4rem">{{ r.company_name }}</span>
              {% endif %}
            </div>
            <span style="font-weight:700;font-size:.9rem;color:var(--{% if r.action=='BUY' %}green{% elif r.action=='SELL' %}red{% else %}amber{% endif %})">
              {{ (r.confidence|float * 100)|int }}% confidence
            </span>
          </div>
          <div class="progress mb-2" style="height:3px">
            <div class="progress-bar" style="width:{{ (r.confidence|float*100)|int }}%;background:var(--{% if r.action=='BUY' %}green{% elif r.action=='SELL' %}red{% else %}amber{% endif %})"></div>
          </div>
          <p style="font-size:.82rem;color:var(--text);margin:0 0 .5rem;line-height:1.6">{{ r.reasoning or '—' }}</p>
          {% if r.signal_sources %}
          <div style="font-size:.73rem;color:var(--muted);border-top:1px solid var(--border);padding-top:.35rem">
            Sources: {{ r.signal_sources }}
          </div>
          {% endif %}
        </div>
        {% endfor %}
        {% else %}
        <div class="text-center py-4" style="color:var(--muted)">
          <div style="font-size:1.6rem;margin-bottom:.4rem">&#128203;</div>
          No recommendations yet today &mdash; pipeline runs at 04:00.
        </div>
        {% endif %}
      </div>
    </div>
  </div>

  {# ── Top scored candidates that fed into TradingAgents ── #}
  <div class="col-md-5">
    <div class="card h-100">
      <div class="card-header">Top Candidates Sent to TradingAgents</div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <thead><tr><th>Ticker</th><th>Direction</th><th>Event</th><th>Score</th></tr></thead>
          <tbody>
          {% for c in candidates %}
          <tr>
            <td>
              <a href="/ticker/{{ c.ticker }}" style="font-weight:700;color:var(--accent);text-decoration:none">{{ c.ticker }}</a>
              {% if c.company_name and c.company_name != c.ticker %}
              <br><small style="color:var(--muted)">{{ c.company_name }}</small>
              {% endif %}
            </td>
            <td><span class="badge badge-{{ c.direction|lower }}" style="font-size:.68rem">{{ c.direction }}</span></td>
            <td><small style="color:var(--muted)">{{ c.event_type }}</small></td>
            <td><strong>{{ "%.0f"|format(c.total_score|float) }}</strong></td>
          </tr>
          {% else %}
          <tr><td colspan="4" class="text-center py-3" style="color:var(--muted)">No candidates today</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="card">
  <div class="card-header">Recent Logs</div>
  <div class="card-body p-0" style="max-height:380px;overflow-y:auto;">
    <table class="table table-sm mb-0">
      <thead><tr><th style="width:70px">Time</th><th style="width:55px">Level</th><th style="width:120px">Component</th><th>Message</th></tr></thead>
      <tbody>
      {% for l in logs %}
      <tr style="background:{{ 'rgba(244,63,94,.06)' if l.level=='ERROR' else ('rgba(245,158,11,.06)' if l.level=='WARNING' else 'transparent') }}">
        <td class="text-nowrap" style="color:var(--sub);font-size:.75rem;font-variant-numeric:tabular-nums">
          {{ l.created_at.strftime('%H:%M:%S') }}
        </td>
        <td>
          {% if l.level == 'ERROR' %}
          <span style="font-size:.68rem;font-weight:700;color:var(--red);background:rgba(244,63,94,.15);padding:.1rem .4rem;border-radius:4px">ERR</span>
          {% elif l.level == 'WARNING' %}
          <span style="font-size:.68rem;font-weight:700;color:var(--amber);background:rgba(245,158,11,.15);padding:.1rem .4rem;border-radius:4px">WARN</span>
          {% else %}
          <span style="font-size:.68rem;font-weight:700;color:var(--green);background:rgba(34,197,94,.12);padding:.1rem .4rem;border-radius:4px">INFO</span>
          {% endif %}
        </td>
        <td style="font-size:.75rem;color:var(--sub)">{{ l.component }}</td>
        <td style="font-size:.82rem;color:#e8edf5">{{ l.message }}</td>
      </tr>
      {% else %}
      <tr><td colspan="4" class="text-center py-4" style="color:var(--muted)">No log entries in the last 24 hours</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
"""

_DATA = """
<div class="d-flex justify-content-between align-items-center mb-3">
  <h5 class="mb-0" style="color:var(--text)">Data Health</h5>
  <small style="color:var(--sub);font-size:.78rem">Live data collected daily</small>
</div>

<div class="card mb-3">
  <div class="card-header d-flex justify-content-between align-items-center">
    <span>LLM Budget (Today)</span>
    <small id="llmBudgetRefresh" style="color:var(--sub);cursor:pointer;font-size:.75rem" onclick="loadLLMBudget()">&#8635; refresh</small>
  </div>
  <div class="card-body" id="llmBudgetBody">
    <div class="row g-3" id="llmBudgetRows">
      <div class="col-12 text-muted" style="font-size:.8rem">Loading…</div>
    </div>
  </div>
</div>
<script>
function loadLLMBudget() {
  fetch('/api/rate-limit-status').then(r=>r.json()).then(function(q) {
    if (!q.ok) { document.getElementById('llmBudgetRows').innerHTML='<div class="col-12 text-danger" style="font-size:.8rem">Error loading budget</div>'; return; }
    var bars = [
      {name:'Groq tokens', used:q.groq_tokens_used, limit:95000, unit:'tok'},
      {name:'OR daily-20 (scheduled)', used:q.or_daily20_used, limit:40, unit:'req'},
      {name:'OR on-demand (manual)', used:q.or_ondemand_used, limit:5, unit:'req'},
    ];
    var html='';
    bars.forEach(function(b) {
      var pct=Math.min(100,Math.round(b.used/b.limit*100));
      var col=pct>=90?'danger':pct>=60?'warning':'success';
      var rem=b.limit-b.used;
      html+='<div class="col-md-4">' +
        '<div class="d-flex justify-content-between mb-1">' +
          '<small><strong>'+b.name+'</strong></small>' +
          '<small class="'+(pct>=90?'text-danger':'text-muted')+'">'+b.used+' / '+b.limit+' '+b.unit+'</small>' +
        '</div>' +
        '<div class="progress" style="height:10px;">' +
          '<div class="progress-bar bg-'+col+'" style="width:'+pct+'%"></div>' +
        '</div>' +
        '<small style="color:var(--sub);font-size:.7rem">'+rem+' remaining</small>' +
      '</div>';
    });
    // Ticker remaining
    var tickRem = q.ondemand_ticker_remaining !== undefined ? q.ondemand_ticker_remaining : '?';
    html += '<div class="col-12 mt-2" style="font-size:.78rem;color:var(--sub)">' +
      'On-demand tickers remaining today: <strong style="color:'+(tickRem===0?'var(--red)':'var(--green)')+'">'+tickRem+'</strong>' +
      ' &nbsp;|&nbsp; Daily-20 tickers done: <strong>'+(q.tickers_daily20_done||0)+'</strong>/20' +
    '</div>';
    document.getElementById('llmBudgetRows').innerHTML=html;
  }).catch(function(){ document.getElementById('llmBudgetRows').innerHTML='<div class="col-12 text-muted" style="font-size:.8rem">Could not load</div>'; });
}
loadLLMBudget();
</script>

<div class="card mb-3">
  <div class="card-header">Other API Budget (Today)</div>
  <div class="card-body">
    <div class="row g-3">
      {% for api in api_budgets %}
      <div class="col-md-4">
        <div class="d-flex justify-content-between mb-1">
          <small><strong>{{ api.name }}</strong></small>
          <small class="{{ 'text-danger' if api.used >= api.limit else 'text-muted' }}">
            {{ api.used }} / {{ api.limit }}
          </small>
        </div>
        <div class="progress" style="height:10px;">
          <div class="progress-bar bg-{{ 'danger' if api.pct >= 90 else ('warning' if api.pct >= 60 else 'success') }}"
               style="width:{{ api.pct }}%;"></div>
        </div>
      </div>
      {% endfor %}
    </div>
  </div>
</div>

<div class="row g-3 mb-3">
  <div class="col-md-4">
    <div class="card h-100">
      <div class="card-header">Universe Stats</div>
      <div class="card-body">
        <div class="mb-2">Total active: <strong>{{ uni_total }}</strong> stocks</div>
        <div class="table-responsive" style="max-height:260px;overflow-y:auto;">
        <table class="table table-sm mb-0">
          <thead><tr><th>Country</th><th>Count</th></tr></thead>
          <tbody>
          {% for r in uni_by_country %}
          <tr><td>{{ r.country or '—' }}</td><td>{{ r.cnt }}</td></tr>
          {% else %}
          <tr><td colspan="2" class="text-muted text-center">No data</td></tr>
          {% endfor %}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  </div>

  <div class="col-md-4">
    <div class="card h-100">
      <div class="card-header">Macro Indicators (FRED)</div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <thead><tr><th>Indicator</th><th>Value</th><th>Date</th></tr></thead>
          <tbody>
          {% for m in macro %}
          <tr style="cursor:pointer" data-bs-toggle="collapse" data-bs-target="#macro-{{ loop.index }}">
            <td>
              <div style="font-size:.82rem;font-weight:600">{{ m.name }}</div>
              {% if m.desc %}
              <div id="macro-{{ loop.index }}" class="collapse" style="font-size:.72rem;color:var(--muted);padding-top:.3rem">
                <div>{{ m.desc[0] }}</div>
                <div style="color:var(--amber);margin-top:.2rem">{{ m.desc[1] }}</div>
              </div>
              {% endif %}
            </td>
            <td><strong>{{ "%.4g"|format(m.value|float) }}</strong></td>
            <td class="text-muted small">{{ m.date }}</td>
          </tr>
          {% else %}
          <tr><td colspan="3" class="text-center text-muted">No macro data</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="col-md-4">
    <div class="card h-100">
      <div class="card-header">News Sentiment (top tickers today)</div>
      <div class="card-body p-0" style="max-height:340px;overflow-y:auto;">
        <table class="table table-sm mb-0">
          <thead><tr><th>Ticker</th><th>Articles</th><th>Tone</th><th>Trend</th></tr></thead>
          <tbody>
          {% for n in news %}
          <tr>
            <td>
              <a href="/ticker/{{ n.ticker }}" style="font-weight:700;color:var(--accent);text-decoration:none">{{ n.ticker }}</a>
              {% if n.company_name and n.company_name != n.ticker %}
              <br><small style="color:var(--muted)">{{ n.company_name }}</small>
              {% endif %}
            </td>
            <td>{{ n.article_count }}</td>
            <td class="{{ 'text-success' if n.avg_tone|float > 0 else 'text-danger' }}">
              {{ "%+.2f"|format(n.avg_tone|float) }}
            </td>
            <td style="color:{% if n.is_accelerating %}var(--green){% else %}var(--muted){% endif %}">
              {{ '↑' if n.is_accelerating else '→' }}</td>
          </tr>
          {% else %}
          <tr><td colspan="4" class="text-center text-muted">No sentiment data today</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="row g-3">
  <div class="col-md-6">
    <div class="card">
      <div class="card-header">Polymarket — Financial Markets Only</div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <thead><tr><th>Market</th><th>Prob</th><th>Tickers/Sectors</th></tr></thead>
          <tbody>
          {% for p in poly %}
          <tr>
            <td><small>{{ p.question[:70] }}</small></td>
            <td>
              <div class="d-flex align-items-center gap-1">
                <div class="progress flex-grow-1" style="height:6px;min-width:40px">
                  <div class="progress-bar" style="width:{{ (p.probability|float * 100)|int }}%;background:#388bfd;"></div>
                </div>
                <small style="white-space:nowrap">{{ (p.probability|float * 100)|int }}%</small>
              </div>
            </td>
            <td style="font-size:.72rem;color:var(--muted)">
              {% if p.relevant_tickers %}
                {% for t in p.relevant_tickers.split(',') if t %}
                <a href="/ticker/{{ t }}" style="color:var(--accent);text-decoration:none">{{ t }}</a>
                {% endfor %}
              {% endif %}
              {% if p.relevant_sectors %}
              <span style="color:var(--muted)">{{ p.relevant_sectors[:40] }}</span>
              {% endif %}
            </td>
          </tr>
          {% else %}
          <tr><td colspan="3" class="text-center text-muted">No financial Polymarket data today</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="col-md-6">
    <div class="card">
      <div class="card-header">Today&#39;s Discovery Candidates</div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <thead><tr><th>Ticker</th><th>Direction</th><th>Event</th><th>Score</th></tr></thead>
          <tbody>
          {% for c in candidates %}
          <tr>
            <td>
              <a href="/ticker/{{ c.ticker }}" style="font-weight:700;color:var(--accent);text-decoration:none">{{ c.ticker }}</a>
              {% if c.company_name and c.company_name != c.ticker %}
              <br><small style="color:var(--muted)">{{ c.company_name }}</small>
              {% endif %}
            </td>
            <td><span class="badge badge-{{ c.direction|lower }}">{{ c.direction }}</span></td>
            <td><small>{{ c.event_type }}</small></td>
            <td>{{ "%.0f"|format(c.total_score|float) }}</td>
          </tr>
          {% else %}
          <tr><td colspan="4" class="text-center text-muted py-2">No candidates yet today</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>
"""

_SIGNALS = """
<h5 class="mb-3" style="color:var(--text)">Today&#39;s Signals Detail</h5>

<div class="row g-3 mb-3">
  <div class="col-md-4">
    <div class="card h-100">
      <div class="card-header">Event Types Detected Today</div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <thead><tr><th>Event Type</th><th>Count</th><th>Avg Score</th></tr></thead>
          <tbody>
          {% for e in events %}
          <tr>
            <td>{{ e.event_type }}</td>
            <td>{{ e.cnt }}</td>
            <td>{{ "%.0f"|format(e.avg_score|float) }}</td>
          </tr>
          {% else %}
          <tr><td colspan="3" class="text-center text-muted py-2">No events today</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="col-md-8">
    <div class="card h-100">
      <div class="card-header">Polymarket Markets &#8594; Stocks</div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <thead><tr><th>Market</th><th>Prob</th><th>Tickers</th></tr></thead>
          <tbody>
          {% for p in poly_stocks %}
          <tr>
            <td><small>{{ p.question[:60] }}</small></td>
            <td>{{ (p.probability|float * 100)|int }}%</td>
            <td><small class="text-muted">{{ p.relevant_tickers or '—' }}</small></td>
          </tr>
          {% else %}
          <tr><td colspan="3" class="text-center text-muted py-2">No market signals today</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="row g-3">
  <div class="col-md-8">
    <div class="card">
      <div class="card-header">Discovery Candidates — Full Detail</div>
      <div class="card-body p-0">
        <div class="table-responsive">
        <table class="table table-sm mb-0">
          <thead><tr>
            <th>Ticker</th><th>Dir</th><th>Event</th>
            <th>Score</th><th>CCY</th><th>FX</th><th>Reason</th>
          </tr></thead>
          <tbody>
          {% for c in candidates %}
          <tr>
            <td>
              <a href="/ticker/{{ c.ticker }}" style="font-weight:700;color:var(--accent);text-decoration:none">{{ c.ticker }}</a>
              {% if c.company_name and c.company_name != c.ticker %}
              <br><small style="color:var(--muted)">{{ c.company_name }}</small>
              {% endif %}
            </td>
            <td>
              {% if c.direction == 'SELL' %}
              <span class="badge bg-secondary" title="Sector headwind — only actionable if held">HEADWIND</span>
              {% else %}
              <span class="badge badge-{{ c.direction|lower }}">{{ c.direction }}</span>
              {% endif %}
            </td>
            <td><small>{{ c.event_type }}</small></td>
            <td>{{ "%.1f"|format(c.total_score|float) }}</td>
            <td>{{ c.currency }}</td>
            <td>{{ c.fx_risk }}</td>
            <td><small title="{{ c.reason or '' }}">{{ (c.reason or '')[:100] }}</small></td>
          </tr>
          {% else %}
          <tr><td colspan="7" class="text-center text-muted py-2">No candidates today</td></tr>
          {% endfor %}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  </div>

  <div class="col-md-4">
    <div class="card">
      <div class="card-header">Finnish Tax Filter Results</div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <thead><tr><th>Ticker</th><th>Status</th><th>Notes</th></tr></thead>
          <tbody>
          {% for c in tax_results %}
          <tr>
            <td>
              <a href="/ticker/{{ c.ticker }}" style="font-weight:700;color:var(--accent);text-decoration:none">{{ c.ticker }}</a>
              {% if c.company_name and c.company_name != c.ticker %}
              <br><small style="color:var(--muted)">{{ c.company_name }}</small>
              {% endif %}
            </td>
            <td>
              <span style="font-size:.7rem;font-weight:700;padding:.15rem .45rem;border-radius:4px;
                           color:{{ '#22c55e' if c.eligible else '#f43f5e' }};
                           background:{{ 'rgba(34,197,94,.15)' if c.eligible else 'rgba(244,63,94,.15)' }}">
                {{ 'ALLOWED' if c.eligible else 'BLOCKED' }}
              </span>
            </td>
            <td><small>{{ (c.tax_notes or '')[:45] }}</small></td>
          </tr>
          {% else %}
          <tr><td colspan="3" class="text-center text-muted py-2">No filter results</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>
"""

_HISTORY = """
<ul class="nav nav-tabs mb-3">
  <li class="nav-item">
    <a class="nav-link active" data-bs-toggle="tab" href="#recTab">Recommendations (30d)</a>
  </li>
  <li class="nav-item">
    <a class="nav-link" data-bs-toggle="tab" href="#tradeTab">Trade Log</a>
  </li>
</ul>
<div class="tab-content">
  <div class="tab-pane fade show active" id="recTab">
    <div class="card">
      <div class="card-body p-0">
        <div class="table-responsive">
        <table class="table table-sm mb-0">
          <thead><tr>
            <th>Date</th><th>Ticker</th><th>Action</th>
            <th>Confidence</th><th>Acted?</th><th>Sources</th><th>Reasoning</th>
          </tr></thead>
          <tbody>
          {% for r in recs %}
          <tr>
            <td class="text-nowrap" style="color:var(--muted);font-size:.78rem">{{ r.date }}</td>
            <td>
              <a href="/ticker/{{ r.ticker }}" style="font-weight:700;color:var(--accent);text-decoration:none">{{ r.ticker }}</a>
              {% if r.company_name and r.company_name != r.ticker %}
              <br><small style="color:var(--muted)">{{ r.company_name }}</small>
              {% endif %}
            </td>
            <td><span class="badge badge-{{ r.action|lower }}">{{ r.action }}</span></td>
            <td>{{ (r.confidence|float * 100)|int }}%</td>
            <td style="color:var(--green)">{{ '✓' if r.acted_on else '' }}</td>
            <td><small style="color:var(--muted)">{{ r.signal_sources or '—' }}</small></td>
            <td><small style="color:var(--muted)">{{ (r.reasoning or '')[:100] }}</small></td>
          </tr>
          {% else %}
          <tr><td colspan="7" class="text-center py-4" style="color:var(--muted)">No recommendations yet</td></tr>
          {% endfor %}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  </div>
  <div class="tab-pane fade" id="tradeTab">
    <div class="card">
      <div class="card-body p-0">
        <div class="table-responsive">
        <table class="table table-sm mb-0">
          <thead><tr>
            <th>Date</th><th>Ticker</th><th>Action</th>
            <th>Shares</th><th>Price</th><th>Currency</th><th>Notes</th>
          </tr></thead>
          <tbody>
          {% for t in trades %}
          <tr>
            <td class="text-nowrap" style="color:var(--muted);font-size:.78rem">{{ t.trade_date }}</td>
            <td>
              <a href="/ticker/{{ t.ticker }}" style="font-weight:700;color:var(--accent);text-decoration:none">{{ t.ticker }}</a>
              {% if t.company_name and t.company_name != t.ticker %}
              <br><small style="color:var(--muted)">{{ t.company_name }}</small>
              {% endif %}
            </td>
            <td><span class="badge badge-{{ t.action|lower }}">{{ t.action }}</span></td>
            <td>{{ "%.4g"|format(t.shares|float) }}</td>
            <td>{{ "%.4f"|format(t.price|float) }}</td>
            <td style="color:var(--muted)">{{ t.currency }}</td>
            <td><small style="color:var(--muted)">{{ t.notes or '—' }}</small></td>
          </tr>
          {% else %}
          <tr><td colspan="7" class="text-center text-muted py-3">No trades logged yet</td></tr>
          {% endfor %}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  </div>
</div>
"""

_SETTINGS = """
<div class="d-flex gap-2 mb-3 flex-wrap">
  <a href="/exposure" class="btn btn-sm btn-outline-primary">&#9881; Sector Allocation Targets</a>
  <a href="/watchlist" class="btn btn-sm btn-outline-secondary">&#128065; Watchlist</a>
  <a href="/history" class="btn btn-sm btn-outline-secondary">&#128203; History</a>
</div>

<div class="row g-3">

  {# ── Simulation Settings ── #}
  <div class="col-md-6">
    <div class="card">
      <div class="card-header">Simulation Settings</div>
      <div class="card-body">
        <form method="POST" action="/settings/simulation">

          <div class="form-check form-switch mb-3">
            <input class="form-check-input" type="checkbox" name="simulate_recommendations"
                   id="simChk" value="true"
                   {{ 'checked' if smap.get('simulate_recommendations') == 'true' else '' }}>
            <label class="form-check-label" for="simChk">
              <strong>Simulate</strong>
              <span style="font-size:.78rem;color:var(--muted)"> — auto-execute recommendations in simulation</span>
            </label>
          </div>

          <div class="mb-3">
            <label class="form-label">Initial Cash (EUR)</label>
            <input type="number" name="simulation_initial_cash" step="any" class="form-control"
                   value="{{ smap.get('simulation_initial_cash') or '2000' }}">
            <div class="form-text">Starting cash for the simulation.</div>
          </div>

          <div class="mb-3">
            <label class="form-label">Monthly Deposit (EUR)</label>
            <input type="number" name="simulation_monthly_deposit" step="any" class="form-control"
                   value="{{ smap.get('simulation_monthly_deposit') or '500' }}">
            <div class="form-text">Auto-deposited on the 15th of each month.</div>
          </div>

          <button type="submit" class="btn btn-primary btn-sm">Save Simulation Settings</button>
        </form>

        <div style="font-size:.8rem;color:var(--muted)" class="mt-2">
          Current balance: <strong style="color:var(--green)">€{{ '%.2f'|format(current_cash) }}</strong>
          — use the <strong>Deposit Cash</strong> button on the home page to add funds.
        </div>
      </div>
    </div>
  </div>

  {# ── Analysis Settings ── #}
  <div class="col-md-6">
    <div class="card">
      <div class="card-header">Analysis Settings</div>
      <div class="card-body">
        <form method="POST" action="/settings">
          {% for key, label, itype, default in fields %}
          <div class="mb-3">
            <label class="form-label">{{ label }}</label>
            {% if itype == 'select' %}
            <select name="{{ key }}" class="form-select">
              {% for opt in ['low', 'medium', 'high'] %}
              <option value="{{ opt }}" {{ 'selected' if (smap.get(key) or default) == opt else '' }}>
                {{ opt|capitalize }}
              </option>
              {% endfor %}
            </select>
            {% elif itype == 'time' %}
            <input type="time" name="{{ key }}"
                   value="{{ smap.get(key) or default }}" class="form-control">
            {% else %}
            <input type="number" name="{{ key }}" step="any"
                   value="{{ smap.get(key) or default }}" class="form-control">
            {% endif %}
          </div>
          {% endfor %}
          <button type="submit" class="btn btn-primary btn-sm">Save Analysis Settings</button>
        </form>
      </div>
    </div>
  </div>

  {# ── Portfolio Constraints ── #}
  <div class="col-md-6">
    <div class="card">
      <div class="card-header">Portfolio Constraints</div>
      <div class="card-body">
        <form method="POST" action="/settings/constraints">
          <div class="mb-3">
            <label class="form-label">Min Trade Value (EUR)</label>
            <input type="number" name="min_trade_value_eur" step="1" class="form-control"
                   value="{{ smap.get('min_trade_value_eur') or '100' }}">
            <div class="form-text">Buys below this are bumped up or demoted to WATCH. Nordnet Level 3 min fee is €7 — €100 keeps fees under 7%.</div>
          </div>
          <div class="mb-3">
            <label class="form-label">Max Holdings</label>
            <input type="number" name="max_holdings" step="1" min="1" max="30" class="form-control"
                   value="{{ smap.get('max_holdings') or '10' }}">
            <div class="form-text">Maximum number of open positions.</div>
          </div>
          <div class="mb-3">
            <label class="form-label">Daily Analysis Count</label>
            <input type="number" name="daily_analysis_count" step="1" min="5" max="50" class="form-control"
                   value="{{ smap.get('daily_analysis_count') or '20' }}">
            <div class="form-text">Tickers run through the full LLM debate each day. Higher = more LLM quota.</div>
          </div>
          <button type="submit" class="btn btn-primary btn-sm">Save Constraints</button>
        </form>
      </div>
    </div>
  </div>

  {# ── Schedule ── #}
  <div class="col-md-6">
    <div class="card">
      <div class="card-header">Pipeline Schedule (UTC)</div>
      <div class="card-body">
        <form method="POST" action="/settings/schedule">
          <div class="mb-3">
            <label class="form-label">Run Days</label>
            <select name="schedule_days" class="form-select">
              {% for val, label in [('1-5','Mon – Fri'),('1-7','Mon – Sun'),('*','Every day')] %}
              <option value="{{ val }}" {{ 'selected' if (smap.get('schedule_days') or '1-5') == val else '' }}>{{ label }}</option>
              {% endfor %}
            </select>
          </div>
          <div class="mb-3">
            <label class="form-label">Daily Pipeline</label>
            <input type="time" name="schedule_daily_time" class="form-control"
                   value="{{ smap.get('schedule_daily_time') or '04:00' }}">
          </div>
          <div class="mb-3">
            <label class="form-label">Morning Brief</label>
            <input type="time" name="schedule_brief_time" class="form-control"
                   value="{{ smap.get('schedule_brief_time') or '07:00' }}">
          </div>
          <div class="mb-3">
            <label class="form-label">Midday Check</label>
            <input type="time" name="schedule_midday_time" class="form-control"
                   value="{{ smap.get('schedule_midday_time') or '12:00' }}">
          </div>
          <button type="submit" class="btn btn-primary btn-sm">Save &amp; Apply Cron</button>
        </form>
        <div class="mt-2" style="font-size:.78rem;color:var(--muted)">
          Saving automatically updates the system crontab.
        </div>
      </div>
    </div>
  </div>

  {# ── Market Selection ── #}
  <div class="col-12">
    <div class="card">
      <div class="card-header">Market Selection</div>
      <div class="card-body">
        <form method="POST" action="/settings/markets">
          <div class="row g-3">
            {% set all_exchanges = [
              ('Americas',  [('NASDAQ','NASDAQ'),('NYSE','NYSE'),('AMEX','AMEX')]),
              ('Germany',   [('XETRA','XETRA'),('FSX','Frankfurt/FSX')]),
              ('UK',        [('LSE','London/LSE')]),
              ('Switzerland',[('SIX','SIX')]),
              ('Finland',   [('HEL','Helsinki/HEL'),('FNFI','First North FI')]),
              ('Sweden',    [('STO','Stockholm/STO'),('FNSE','First North SE')]),
              ('Denmark',   [('CPH','Copenhagen/CPH'),('FNDK','First North DK')]),
            ] %}
            {% set uni_set = (smap.get('universe_markets') or 'NASDAQ,NYSE,AMEX,XETRA,LSE,SIX,HEL,STO,CPH').split(',') | map('trim') | list %}
            {% set ana_set = (smap.get('analysis_markets') or '').split(',') | map('trim') | list %}

            <div class="col-md-6">
              <label class="form-label fw-semibold">Universe Markets <small class="text-muted">(download &amp; validate)</small></label>
              <div class="row g-1">
                {% for region, exchanges in all_exchanges %}
                <div class="col-12"><small class="text-muted">{{ region }}</small></div>
                {% for code, label in exchanges %}
                <div class="col-auto">
                  <div class="form-check form-check-inline">
                    <input class="form-check-input" type="checkbox" name="universe_markets"
                           value="{{ code }}" id="uni_{{ code }}"
                           {{ 'checked' if code in uni_set else '' }}>
                    <label class="form-check-label" for="uni_{{ code }}" style="font-size:.85rem">{{ label }}</label>
                  </div>
                </div>
                {% endfor %}
                {% endfor %}
              </div>
            </div>

            <div class="col-md-6">
              <label class="form-label fw-semibold">Analysis Markets <small class="text-muted">(suggestions drawn from)</small></label>
              <div class="form-check mb-2">
                <input class="form-check-input" type="checkbox" id="anaAll" onchange="toggleAnaAll(this)"
                       {{ 'checked' if not smap.get('analysis_markets') else '' }}>
                <label class="form-check-label" for="anaAll" style="font-size:.85rem">Same as Universe Markets</label>
              </div>
              <div id="anaMarkets" style="{{ 'display:none' if not smap.get('analysis_markets') else '' }}">
              <div class="row g-1">
                {% for region, exchanges in all_exchanges %}
                <div class="col-12"><small class="text-muted">{{ region }}</small></div>
                {% for code, label in exchanges %}
                <div class="col-auto">
                  <div class="form-check form-check-inline">
                    <input class="form-check-input" type="checkbox" name="analysis_markets"
                           value="{{ code }}" id="ana_{{ code }}"
                           {{ 'checked' if code in ana_set else '' }}>
                    <label class="form-check-label" for="ana_{{ code }}" style="font-size:.85rem">{{ label }}</label>
                  </div>
                </div>
                {% endfor %}
                {% endfor %}
              </div>
              </div>
            </div>

          </div>
          <button type="submit" class="btn btn-primary btn-sm mt-3">Save Markets</button>
        </form>
        <div class="mt-2" style="font-size:.78rem;color:var(--muted)">
          Universe changes take effect on the next monthly refresh. Analysis market changes apply from the next daily run.
        </div>
      </div>
    </div>
  </div>

<script>
function toggleAnaAll(cb) {
  document.getElementById('anaMarkets').style.display = cb.checked ? 'none' : '';
}
</script>

  {# ── Danger Zone ── #}
  <div class="col-12">
    <div class="card" style="border-color:var(--red)">
      <div class="card-header" style="background:var(--red-dim,rgba(220,53,69,.12));color:var(--red);font-weight:600">
        &#9888; Danger Zone
      </div>
      <div class="card-body">
        <div class="d-flex align-items-start gap-3 flex-wrap">
          <div style="flex:1;min-width:220px">
            <strong>Reset &amp; Start Fresh</strong>
            <p class="text-muted mb-0" style="font-size:.82rem;margin-top:.3rem">
              Wipes all holdings, trades, recommendations, watchlist, discovery candidates
              and penalties. Resets cash to the simulation initial cash setting.
              <strong>This cannot be undone.</strong>
            </p>
          </div>
          <button class="btn btn-outline-danger btn-sm align-self-center"
                  onclick="document.getElementById('resetModal').style.display='flex'">
            Reset Everything
          </button>
        </div>
      </div>
    </div>
  </div>

  {# ── Reset confirmation modal ── #}
  <div id="resetModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);
       z-index:9999;align-items:center;justify-content:center">
    <div class="card" style="max-width:420px;width:90%;border-color:var(--red)">
      <div class="card-header" style="background:var(--red);color:#fff;font-weight:600">
        &#9888;&nbsp; Confirm Full Reset
      </div>
      <div class="card-body">
        <p style="font-size:.9rem">This will permanently delete:</p>
        <ul style="font-size:.85rem;color:var(--muted)">
          <li>All holdings &amp; trade history</li>
          <li>All recommendations</li>
          <li>All watchlist entries</li>
          <li>All discovery candidates &amp; penalties</li>
          <li>Performance history &amp; strategy metrics</li>
        </ul>
        <p style="font-size:.82rem;color:var(--green)">
          &#10003; Universe, prices, sentiment &amp; macro data are <strong>kept</strong>.
        </p>
        <p style="font-size:.85rem">Cash will be reset to
          <strong>€{{ smap.get('simulation_initial_cash') or '2000' }}</strong>
          (your simulation initial cash setting).</p>
        <p style="font-size:.85rem;color:var(--red)"><strong>This cannot be undone.</strong></p>
        <div class="d-flex gap-2 justify-content-end mt-3">
          <button class="btn btn-sm btn-secondary"
                  onclick="document.getElementById('resetModal').style.display='none'">
            Cancel
          </button>
          <form method="POST" action="/settings/reset" style="margin:0">
            <button type="submit" class="btn btn-sm btn-danger">
              Yes, reset everything
            </button>
          </form>
        </div>
      </div>
    </div>
  </div>

  {# ── LLM Budget ── #}
  <div class="col-12">
    <div class="card">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span>LLM API Usage (Today)</span>
        <small id="settBudgetRefresh" style="color:var(--sub);cursor:pointer;font-size:.75rem" onclick="loadSettBudget()">&#8635; refresh</small>
      </div>
      <div class="card-body">
        <div class="row g-3" id="settBudgetRows">
          <div class="col-12 text-muted" style="font-size:.8rem">Loading…</div>
        </div>
      </div>
    </div>
  </div>

</div>

<script>
function loadSettBudget() {
  fetch('/api/rate-limit-status').then(r=>r.json()).then(function(q) {
    if (!q.ok) { document.getElementById('settBudgetRows').innerHTML='<div class="col-12 text-danger">Error</div>'; return; }
    var bars = [
      {name:'Groq tokens/day', used:q.groq_tokens_used, limit:95000, unit:'tok'},
      {name:'OpenRouter scheduled', used:q.or_daily20_used, limit:40, unit:'req'},
      {name:'OpenRouter on-demand', used:q.or_ondemand_used, limit:5, unit:'req'},
    ];
    var html='';
    bars.forEach(function(b) {
      var pct=Math.min(100,Math.round(b.used/b.limit*100));
      var col=pct>=90?'danger':pct>=60?'warning':'success';
      html+='<div class="col-md-4">' +
        '<div class="d-flex justify-content-between mb-1">' +
          '<small><strong>'+b.name+'</strong></small>' +
          '<small class="'+(pct>=90?'text-danger':'text-muted')+'">'+b.used+'/'+b.limit+' '+b.unit+'</small>' +
        '</div>' +
        '<div class="progress" style="height:8px;"><div class="progress-bar bg-'+col+'" style="width:'+pct+'%"></div></div>' +
        '<small style="color:var(--sub);font-size:.7rem">'+(b.limit-b.used)+' remaining</small>' +
      '</div>';
    });
    html += '<div class="col-12 mt-1" style="font-size:.78rem;color:var(--sub)">' +
      'On-demand tickers remaining: <strong>' + (q.ondemand_ticker_remaining||0) + '</strong>' +
      ' &nbsp;|&nbsp; Scheduled tickers done today: <strong>' + (q.tickers_daily20_done||0) + '</strong>/20' +
    '</div>';
    document.getElementById('settBudgetRows').innerHTML=html;
  }).catch(function(){});
}
loadSettBudget();
</script>
"""

_WATCHLIST = """
<div class="row g-3">
  <div class="col-12">
    <div class="text-muted small mb-1">
      Universe: <strong>{{ uni_count }}</strong> stocks
      {% if uni_updated %} &middot; Last updated: {{ uni_updated }}{% endif %}
    </div>
  </div>

  <div class="col-md-8">
    <div class="card">
      <div class="card-header">Active Watchlist ({{ watchlist|length }} stocks)</div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <thead><tr>
            <th>Ticker</th><th>Company</th><th>Sector</th><th>Last Tone</th><th></th>
          </tr></thead>
          <tbody>
          {% for w in watchlist %}
          <tr>
            <td><a href="/ticker/{{ w.ticker }}" style="font-weight:700;color:var(--accent);text-decoration:none">{{ w.ticker }}</a></td>
            <td>{{ w.company_name or '—' }}</td>
            <td><small>{{ w.sector or '—' }}</small></td>
            <td>
              {% if w.avg_tone is not none %}
              <span class="{{ 'text-success' if w.avg_tone|float > 0 else 'text-danger' }}">
                {{ "%+.2f"|format(w.avg_tone|float) }}
              </span>
              {% else %}—{% endif %}
            </td>
            <td>
              <form method="POST" action="/watchlist/remove" style="display:inline">
                <input type="hidden" name="ticker" value="{{ w.ticker }}">
                <button class="btn btn-sm btn-outline-danger py-0">Remove</button>
              </form>
            </td>
          </tr>
          {% else %}
          <tr><td colspan="5" class="text-center text-muted py-3">No watchlist items</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="col-md-4">
    <div class="card">
      <div class="card-header">Add to Watchlist</div>
      <div class="card-body">
        <form method="POST" action="/watchlist/add">
          <div class="mb-3">
            <label class="form-label">Ticker</label>
            <input name="ticker" class="form-control" placeholder="e.g. AAPL" required>
          </div>
          <div class="mb-3">
            <label class="form-label">Company Name (optional)</label>
            <input name="company_name" class="form-control">
          </div>
          <button type="submit" class="btn btn-success w-100">Add to Watchlist</button>
        </form>
      </div>
    </div>
  </div>
</div>
"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    holdings, total_value = get_holdings_enriched()
    recs = query("""
        SELECT DISTINCT ON (r.ticker) r.ticker, r.action, r.confidence, r.reasoning, r.signal_sources,
               r.acted_on, r.bull_case, r.bear_case, r.key_risks,
               COALESCE(u.company_name, w.company_name, r.ticker) AS company_name
        FROM recommendations r
        LEFT JOIN universe u ON r.ticker = u.ticker
        LEFT JOIN watchlist w ON r.ticker = w.ticker
        WHERE r.date=%s AND r.action != 'HOLD'
        ORDER BY r.ticker, r.confidence DESC
    """, (date.today(),))
    cash = get_cash()
    fx = get_live_fx()

    # Build execute-button data for each signal
    holdings_map = {h['ticker']: h for h in holdings}
    holdings_tickers = set(holdings_map.keys())

    rec_prices, rec_shares_map, rec_currency_map = {}, {}, {}
    for r in recs:
        t = r['ticker']
        pr = query("SELECT close FROM prices WHERE ticker=%s ORDER BY date DESC LIMIT 1", (t,))
        price = round(float(pr[0]['close']), 2) if pr else 0.0
        rec_prices[t] = price

        # Determine currency from holdings or universe
        uni = query("SELECT country FROM universe WHERE ticker=%s LIMIT 1", (t,))
        country = uni[0]['country'] if uni else 'US'
        ccy = 'EUR' if country in ('FI','DE','FR','NL','ES','IT','BE','AT','PT') else \
              'GBP' if country == 'GB' else \
              'SEK' if country == 'SE' else \
              'JPY' if country == 'JP' else 'USD'
        rec_currency_map[t] = ccy

        if r['action'] == 'BUY' and price > 0:
            rate = fx.get(ccy, fx.get('USD', 0.853))
            budget = min(cash * 0.15, 500)  # up to 15% of cash or €500
            rec_shares_map[t] = max(1, round(budget / (price * rate), 2)) if rate else 0
        elif r['action'] == 'SELL' and t in holdings_map:
            rec_shares_map[t] = float(holdings_map[t]['shares'])
        else:
            rec_shares_map[t] = 0

    # Sector + regional exposure for overview
    sector_exposure = _get_current_sector_exposure()
    sector_targets_val = _get_sector_targets_db()
    try:
        from portfolio.portfolio_brain import _load_holdings, _region_exposure
        region_exposure = _region_exposure(_load_holdings())
    except Exception:
        region_exposure = {}

    # Enrich recs with natural language summaries
    for r in recs:
        r['natural_summary'] = _natural_summary(r)

    # Map holding tickers to today's rec for quick status column
    holdings_rec_map = {r['ticker']: r for r in recs}

    # Market regime
    regime_class, regime_icon, regime_label, regime_desc = _get_market_regime()

    return render_template_string(
        make_page(_INDEX, 'Today'),
        current_page='Today',
        holdings=holdings, total_value=total_value,
        recs=recs, cash=cash,
        holdings_tickers=holdings_tickers,
        holdings_rec_map=holdings_rec_map,
        rec_prices=rec_prices, rec_shares_map=rec_shares_map, rec_currency_map=rec_currency_map,
        pipeline_running=_pipeline_is_running(),
        sector_exposure=sector_exposure, sector_targets=sector_targets_val,
        region_exposure=region_exposure,
        regime_class=regime_class, regime_icon=regime_icon,
        regime_label=regime_label, regime_desc=regime_desc,
        today=date.today().isoformat(),
        health=get_system_health(),
    )


@app.route('/pipeline')
def pipeline():
    stages = get_pipeline_status()
    logs = query("""
        SELECT component, level, message, created_at FROM system_logs
        ORDER BY created_at DESC LIMIT 40
    """)
    recs = query("""
        SELECT DISTINCT ON (r.ticker) r.ticker, r.action, r.confidence, r.reasoning, r.signal_sources,
               COALESCE(u.company_name, r.ticker) AS company_name
        FROM recommendations r
        LEFT JOIN universe u ON r.ticker = u.ticker
        WHERE r.date = %s AND r.action != 'HOLD'
        ORDER BY r.ticker, r.confidence DESC
    """, (date.today(),))
    # Load the actual set sent to trading agents (written by daily_selection.py)
    _sel_row = query("SELECT value FROM user_settings WHERE key='last_daily_selection'")
    candidates = []
    if _sel_row and _sel_row[0]['value']:
        import json as _json
        _sel = _json.loads(_sel_row[0]['value'])
        if _sel.get('date') == str(date.today()):
            # Enrich with company_name from universe
            tickers = [i['ticker'] for i in _sel.get('tickers', [])]
            if tickers:
                _meta = query("SELECT ticker, company_name FROM universe WHERE ticker IN %s", (tuple(tickers),))
                _meta_map = {r['ticker']: r['company_name'] for r in _meta}
            else:
                _meta_map = {}
            for item in _sel.get('tickers', []):
                candidates.append({
                    'ticker':       item['ticker'],
                    'direction':    'BUY',
                    'event_type':   item['state'],
                    'total_score':  item['conviction'] * 100,
                    'company_name': _meta_map.get(item['ticker'], item['ticker']),
                })
    pipeline_running = _pipeline_is_running()
    return render_template_string(
        make_page(_PIPELINE, 'Pipeline'),
        current_page='Pipeline',
        stages=stages, logs=logs, recs=recs, candidates=candidates,
        pipeline_running=pipeline_running,
        health=get_system_health(),
    )


@app.route('/research')
def research():
    return redirect(url_for('data_health'))


@app.route('/data')
def data_health():
    uni_row = query("SELECT COUNT(*) AS cnt FROM universe WHERE active=TRUE")
    uni_total = int(uni_row[0]['cnt']) if uni_row else 0

    uni_by_country = query("""
        SELECT country, COUNT(*) AS cnt FROM universe
        WHERE active=TRUE GROUP BY country ORDER BY cnt DESC
    """)

    macro_raw = query("""
        SELECT DISTINCT ON (series_id) series_id, value, date
        FROM macro_data ORDER BY series_id, date DESC
    """)
    macro = [dict(r,
                  name=MACRO_NAMES.get(r['series_id'], r['series_id']),
                  desc=MACRO_DESC.get(r['series_id']))
             for r in macro_raw]

    news = query("""
        SELECT DISTINCT ON (ns.ticker) ns.ticker, ns.article_count, ns.avg_tone, ns.is_accelerating,
               COALESCE(u.company_name, ns.ticker) AS company_name
        FROM news_sentiment ns
        LEFT JOIN universe u ON ns.ticker = u.ticker
        ORDER BY ns.ticker, ns.date DESC LIMIT 20
    """)

    poly = query("""
        SELECT DISTINCT ON (question) question, MAX(probability) AS probability,
               relevant_tickers, relevant_sectors
        FROM polymarket_signals
        WHERE relevant_tickers != ''
        GROUP BY question, relevant_tickers, relevant_sectors
        ORDER BY question, probability DESC LIMIT 20
    """)

    candidates = query("""
        SELECT * FROM (
            SELECT DISTINCT ON (dc.ticker)
                   dc.ticker, dc.direction, dc.event_type, dc.total_score,
                   COALESCE(u.company_name, dc.ticker) AS company_name
            FROM discovery_candidates dc
            LEFT JOIN universe u ON dc.ticker = u.ticker
            WHERE dc.date=%s
            ORDER BY dc.ticker, dc.total_score DESC
        ) sub ORDER BY total_score DESC
    """, (date.today(),))

    # API budget widget
    API_LIMITS = {
        'alpha_vantage': ('Alpha Vantage', 22),
        'yfinance':      ('yfinance',      200),
        'gdelt':         ('GDELT',         50),
    }
    usage_rows = query("""
        SELECT api_name, SUM(call_count) AS total
        FROM api_usage WHERE date=%s GROUP BY api_name
    """, (date.today(),))
    usage_map = {r['api_name']: int(r['total'] or 0) for r in usage_rows}
    api_budgets = []
    for key, (name, limit) in API_LIMITS.items():
        used = usage_map.get(key, 0)
        pct = min(100, int(used / limit * 100))
        api_budgets.append({'name': name, 'used': used, 'limit': limit, 'pct': pct})

    return render_template_string(
        make_page(_RESEARCH_SUBNAV + _DATA, 'Research — Data'),
        current_page='Research', research_page='data',
        uni_total=uni_total, uni_by_country=uni_by_country,
        macro=macro, news=news, poly=poly, candidates=candidates,
        api_budgets=api_budgets,
        health=get_system_health(),
    )


@app.route('/signals')
def signals():
    events = query("""
        SELECT event_type, COUNT(*) AS cnt, AVG(total_score) AS avg_score
        FROM discovery_candidates WHERE date=%s
        GROUP BY event_type ORDER BY cnt DESC
    """, (date.today(),))

    poly_stocks = query("""
        SELECT DISTINCT ON (question) question, MAX(probability) AS probability,
               relevant_tickers, relevant_sectors
        FROM polymarket_signals WHERE date >= CURRENT_DATE - INTERVAL '3 days'
          AND relevant_tickers != ''
        GROUP BY question, relevant_tickers, relevant_sectors
        ORDER BY question, probability DESC LIMIT 20
    """)
    if not poly_stocks:
        poly_stocks = query("""
            SELECT DISTINCT ON (question) question, MAX(probability) AS probability,
                   relevant_tickers, relevant_sectors
            FROM polymarket_signals
            WHERE relevant_tickers != ''
            GROUP BY question, relevant_tickers, relevant_sectors
            ORDER BY question, probability DESC LIMIT 20
        """)

    candidates = query("""
        SELECT * FROM (
            SELECT DISTINCT ON (dc.ticker)
                   dc.ticker, dc.direction, dc.event_type, dc.total_score,
                   dc.currency, dc.fx_risk, dc.reason,
                   COALESCE(u.company_name, dc.ticker) AS company_name
            FROM discovery_candidates dc
            LEFT JOIN universe u ON dc.ticker = u.ticker
            WHERE dc.date=%s
            ORDER BY dc.ticker, dc.total_score DESC
        ) sub ORDER BY total_score DESC
    """, (date.today(),))

    tax_results = query("""
        SELECT * FROM (
            SELECT DISTINCT ON (dc.ticker)
                   dc.ticker, dc.eligible, dc.tax_notes,
                   COALESCE(u.company_name, dc.ticker) AS company_name
            FROM discovery_candidates dc
            LEFT JOIN universe u ON dc.ticker = u.ticker
            WHERE dc.date=%s
            ORDER BY dc.ticker, dc.total_score DESC
        ) sub ORDER BY eligible DESC, ticker
    """, (date.today(),))

    return render_template_string(
        make_page(_RESEARCH_SUBNAV + _SIGNALS, 'Research — Signals'),
        current_page='Research', research_page='signals',
        events=events, poly_stocks=poly_stocks,
        candidates=candidates, tax_results=tax_results,
        health=get_system_health(),
    )


@app.route('/history')
def history():
    recs = query("""
        SELECT r.date, r.ticker, r.action, r.confidence, r.reasoning, r.acted_on, r.signal_sources,
               COALESCE(u.company_name, r.ticker) AS company_name
        FROM recommendations r
        LEFT JOIN universe u ON r.ticker = u.ticker
        WHERE r.date >= %s ORDER BY r.date DESC, r.confidence DESC LIMIT 200
    """, (date.today() - timedelta(days=30),))

    trades = query("""
        SELECT t.ticker, t.action, t.shares, t.price, t.currency, t.trade_date, t.notes,
               COALESCE(u.company_name, t.ticker) AS company_name
        FROM trades t
        LEFT JOIN universe u ON t.ticker = u.ticker
        ORDER BY t.trade_date DESC, t.id DESC LIMIT 200
    """)

    return render_template_string(
        make_page(_HISTORY, 'History'),
        current_page='History',

        recs=recs, trades=trades,
        health=get_system_health(),
    )


@app.route('/trade', methods=['POST'])
def log_trade():
    ticker = request.form.get('ticker', '').upper().strip()
    action = request.form.get('action', 'BUY').upper()
    shares = float(request.form.get('shares', 0) or 0)
    price = float(request.form.get('price', 0) or 0)
    currency = request.form.get('currency', 'EUR').upper().strip() or 'EUR'
    notes = request.form.get('notes', '').strip()
    trade_date_str = request.form.get('trade_date', '').strip()

    try:
        trade_date = date.fromisoformat(trade_date_str) if trade_date_str else date.today()
    except ValueError:
        trade_date = date.today()

    if not ticker or shares <= 0 or price <= 0:
        flash('Invalid trade: ticker, shares and price are required.')
        return redirect(url_for('index'))

    execute("""
        INSERT INTO trades (ticker, action, shares, price, currency, trade_date, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (ticker, action, shares, price, currency, trade_date, notes or None))

    fx = get_live_fx()
    rate = fx.get(currency, fx.get('USD', 0.853))
    cost_eur = shares * price * rate

    if action == 'BUY':
        existing = query(
            "SELECT shares, avg_buy_price FROM holdings WHERE ticker=%s AND active=TRUE",
            (ticker,))
        if existing:
            old_s = float(existing[0]['shares'])
            old_p = float(existing[0]['avg_buy_price'] or price)
            new_s = old_s + shares
            new_avg = (old_s * old_p + shares * price) / new_s
            execute("""
                UPDATE holdings SET shares=%s, avg_buy_price=%s, updated_at=NOW()
                WHERE ticker=%s AND active=TRUE
            """, (new_s, new_avg, ticker))
        else:
            execute("""
                INSERT INTO holdings (ticker, shares, avg_buy_price, currency, bought_date)
                VALUES (%s, %s, %s, %s, %s)
            """, (ticker, shares, price, currency, trade_date))

        # Deduct from cash
        current_cash = get_cash()
        new_cash = max(0.0, current_cash - cost_eur)
        _upsert_setting('nordnet_cash_eur', str(round(new_cash, 2)))
        cash_note = f' | Cash: €{current_cash:.0f} → €{new_cash:.0f}'

    elif action == 'SELL':
        existing = query(
            "SELECT shares FROM holdings WHERE ticker=%s AND active=TRUE", (ticker,))
        if existing:
            new_s = float(existing[0]['shares']) - shares
            if new_s <= 0:
                execute("UPDATE holdings SET active=FALSE, updated_at=NOW() WHERE ticker=%s", (ticker,))
            else:
                execute("""
                    UPDATE holdings SET shares=%s, updated_at=NOW()
                    WHERE ticker=%s AND active=TRUE
                """, (new_s, ticker))

        # Add proceeds to cash
        current_cash = get_cash()
        new_cash = current_cash + cost_eur
        _upsert_setting('nordnet_cash_eur', str(round(new_cash, 2)))
        cash_note = f' | Cash: €{current_cash:.0f} → €{new_cash:.0f}'
    else:
        cash_note = ''

    log('INFO', 'dashboard', f'Trade logged: {action} {shares} {ticker} @ {price} {currency}{cash_note}')
    # Mark recommendation acted_on if one exists today
    execute("UPDATE recommendations SET acted_on=TRUE WHERE ticker=%s AND date=%s",
            (ticker, date.today()))
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or \
              request.accept_mimetypes.accept_json
    if is_ajax:
        from flask import jsonify as _json
        return _json({'ok': True, 'msg': f'{action} {shares} {ticker} @ {price} {currency}{cash_note}'})
    flash(f'Trade logged: {action} {shares} {ticker} @ {price} {currency}{cash_note}')
    ref = request.referrer or url_for('index')
    return redirect(ref)


def _upsert_setting(key, value):
    execute("""
        INSERT INTO user_settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
    """, (key, value))


def _maybe_monthly_deposit(smap=None):
    """Deposit is now handled by auto_executor._maybe_monthly_deposit() in the daily pipeline.
    This stub is kept so the Settings page load doesn't error; it is a no-op."""
    pass


@app.route('/settings/simulation', methods=['POST'])
def settings_simulation():
    simulate = 'true' if request.form.get('simulate_recommendations') == 'true' else 'false'
    _upsert_setting('simulate_recommendations', simulate)
    initial = request.form.get('simulation_initial_cash', '2000')
    monthly = request.form.get('simulation_monthly_deposit', '500')
    _upsert_setting('simulation_initial_cash', initial)
    _upsert_setting('simulation_monthly_deposit', monthly)
    # Keep brain's budget key in sync so portfolio_brain always has a fallback
    _upsert_setting('monthly_investment_budget_eur', monthly)
    # If this is the first time enabling, initialise cash from initial_cash if balance is 0
    if simulate == 'true':
        current = get_cash()
        if current == 0:
            _upsert_setting('nordnet_cash_eur', initial)
            flash(f'Simulation enabled. Cash initialised to €{float(initial):,.0f}.')
        else:
            flash('Simulation settings saved.')
    else:
        flash('Simulation disabled.')
    log('INFO', 'dashboard', f'Simulation settings updated: simulate={simulate}')
    return redirect(url_for('settings'))


@app.route('/settings/constraints', methods=['POST'])
def settings_constraints():
    for key in ('min_trade_value_eur', 'max_holdings', 'daily_analysis_count'):
        val = request.form.get(key, '').strip()
        if val:
            _upsert_setting(key, val)
    log('INFO', 'dashboard', 'Portfolio constraints updated')
    flash('Portfolio constraints saved.')
    return redirect(url_for('settings'))


@app.route('/settings/schedule', methods=['POST'])
def settings_schedule():
    import subprocess
    keys = ('schedule_days', 'schedule_daily_time', 'schedule_brief_time', 'schedule_midday_time')
    vals = {k: request.form.get(k, '').strip() for k in keys}
    for k, v in vals.items():
        if v:
            _upsert_setting(k, v)

    days   = vals.get('schedule_days') or '1-5'
    daily  = vals.get('schedule_daily_time') or '04:00'
    brief  = vals.get('schedule_brief_time') or '07:00'
    midday = vals.get('schedule_midday_time') or '12:00'

    def to_cron(t):
        h, m = t.split(':')
        return f'{int(m)} {int(h)}'

    advisor = '/home/ubuntu/advisor'
    venv    = '/home/ubuntu/venv'
    entries = [
        f"{to_cron(daily)} * * {days} {advisor}/run_daily.sh >> {advisor}/logs/daily.log 2>&1",
        f"{to_cron(brief)} * * {days} {advisor}/send_brief.sh >> {advisor}/logs/brief.log 2>&1",
        f"{to_cron(midday)} * * {days} {advisor}/run_midday.sh >> {advisor}/logs/midday.log 2>&1",
        f"0 5 * * 0 source {venv}/bin/activate && cd {advisor} && python pipeline/backtester.py >> {advisor}/logs/backtest.log 2>&1",
    ]
    new_cron = '\n'.join(entries)
    script = f"(crontab -l 2>/dev/null | grep -v '{advisor}' || true; printf '%s\\n' {chr(39)}{new_cron}{chr(39)}) | crontab -"
    try:
        subprocess.run(['bash', '-c', script], check=True, timeout=10)
        log('INFO', 'dashboard', f'Cron updated: daily={daily} brief={brief} midday={midday} days={days}')
        flash(f'Schedule saved and cron updated — daily {daily}, brief {brief}, midday {midday} UTC on days {days}.')
    except Exception as e:
        log('WARNING', 'dashboard', f'Cron update failed: {e}')
        flash(f'Settings saved but cron update failed: {e}')
    return redirect(url_for('settings'))


@app.route('/settings/markets', methods=['POST'])
def settings_markets():
    uni = ','.join(v.strip() for v in request.form.getlist('universe_markets') if v.strip())
    ana = ','.join(v.strip() for v in request.form.getlist('analysis_markets') if v.strip())
    if uni:
        _upsert_setting('universe_markets', uni)
    _upsert_setting('analysis_markets', ana)  # empty string = same as universe
    log('INFO', 'dashboard', f'Markets updated: universe={uni or "all"} analysis={ana or "same as universe"}')
    flash('Market selection saved.')
    return redirect(url_for('settings'))


@app.route('/settings/reset', methods=['POST'])
def settings_reset():
    try:
        rows = query("SELECT value FROM user_settings WHERE key='simulation_initial_cash'")
        initial_cash = float(rows[0]['value']) if rows and rows[0]['value'] else 2000.0

        # Simulation state only — universe, prices, sentiment, macro data are preserved
        execute("DELETE FROM holdings")
        execute("DELETE FROM trades")
        execute("DELETE FROM recommendations")
        execute("DELETE FROM watchlist")
        execute("DELETE FROM discovery_candidates")
        execute("DELETE FROM performance_history")
        execute("DELETE FROM strategy_metrics")

        _upsert_setting('nordnet_cash_eur', str(initial_cash))
        _upsert_setting('monthly_invested_this_month', '0')

        log('INFO', 'dashboard', f'Full reset performed — cash reset to €{initial_cash:.0f}')
        flash(f'Reset complete. All data cleared. Cash set to €{initial_cash:,.0f}.')
    except Exception as e:
        flash(f'Reset failed: {e}')
    return redirect(url_for('settings'))


@app.route('/add-cash', methods=['POST'])
def add_cash():
    try:
        amount = float(request.form.get('amount', 0) or 0)
        if amount <= 0:
            flash('Enter a positive amount.')
            return redirect(url_for('settings'))
        current = get_cash()
        new_bal = round(current + amount, 2)
        _upsert_setting('nordnet_cash_eur', str(new_bal))
        log('INFO', 'dashboard', f'Manual cash deposit €{amount:.2f} — balance €{new_bal:.2f}')
        flash(f'Added €{amount:,.2f} — new balance €{new_bal:,.2f}')
    except Exception as e:
        flash(f'Failed: {e}')
    return redirect(url_for('settings'))


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        for key, value in request.form.items():
            _upsert_setting(key, value)
        log('INFO', 'dashboard', 'Settings updated')
        flash('Settings saved.')
        return redirect(url_for('settings'))

    rows = query("SELECT key, value FROM user_settings")
    smap = {r['key']: r['value'] for r in rows}
    _maybe_monthly_deposit(smap)
    # Reload after potential deposit
    rows = query("SELECT key, value FROM user_settings")
    smap = {r['key']: r['value'] for r in rows}
    return render_template_string(
        make_page(_SETTINGS, 'Settings'),
        current_page='Settings',
        fields=SETTINGS_META, smap=smap,
        current_cash=get_cash(),
        health=get_system_health(),
    )


from portfolio.sector_utils import ALL_SECTORS, normalize as _normalize_sector


def _get_sector_targets_db():
    import json as _json
    row = query("SELECT value FROM user_settings WHERE key='sector_targets'")
    if row and row[0]['value']:
        try:
            return _json.loads(row[0]['value'])
        except Exception:
            pass
    return {s: round(100 / len(ALL_SECTORS)) for s in ALL_SECTORS}


def _get_current_region_exposure():
    """Calculate current portfolio region breakdown from holdings."""
    from portfolio.sector_utils import country_to_region
    holdings = query("""
        SELECT h.ticker, h.shares, h.avg_buy_price, h.currency, u.country
        FROM holdings h
        LEFT JOIN universe u ON h.ticker = u.ticker
        WHERE h.active = TRUE
    """)
    if not holdings:
        return {}
    fx = get_live_fx()
    total_eur = 0.0
    region_eur = {}
    for h in holdings:
        pr = query("SELECT close FROM prices WHERE ticker=%s ORDER BY date DESC LIMIT 1", (h['ticker'],))
        price = float(pr[0]['close']) if pr else float(h['avg_buy_price'] or 0)
        ccy = h['currency'] or 'USD'
        rate = fx.get(ccy, fx.get('USD', 0.85))
        value = float(h['shares']) * price * rate
        region = country_to_region((h['country'] or 'US').strip())
        region_eur[region] = region_eur.get(region, 0) + value
        total_eur += value
    if not total_eur:
        return {}
    return {r: round(v / total_eur * 100, 1) for r, v in region_eur.items()}


def _get_current_sector_exposure():
    """Calculate current portfolio sector breakdown from holdings."""
    holdings = query("""
        SELECT h.ticker, h.shares, h.avg_buy_price, h.currency, u.sector
        FROM holdings h
        LEFT JOIN universe u ON h.ticker = u.ticker
        WHERE h.active = TRUE
    """)
    if not holdings:
        return {}
    fx = get_live_fx()
    total_eur = 0.0
    sector_eur = {}
    for h in holdings:
        pr = query("SELECT close FROM prices WHERE ticker=%s ORDER BY date DESC LIMIT 1", (h['ticker'],))
        price = float(pr[0]['close']) if pr else float(h['avg_buy_price'] or 0)
        ccy = h['currency'] or 'USD'
        rate = fx.get(ccy, fx.get('USD', 0.85))
        value = float(h['shares']) * price * rate
        sector = _normalize_sector((h['sector'] or 'Unknown').strip())
        sector_eur[sector] = sector_eur.get(sector, 0) + value
        total_eur += value
    if not total_eur:
        return {}
    return {s: round(v / total_eur * 100, 1) for s, v in sector_eur.items()}


@app.route('/exposure', methods=['GET'])
def exposure():
    targets = _get_sector_targets_db()
    current = _get_current_sector_exposure()
    cooldown = query("SELECT value FROM user_settings WHERE key='ticker_cooldown_days'")
    cooldown_days = int(cooldown[0]['value']) if cooldown else 5

    # Recent rebalance suggestions (last 24h)
    rebalance_recs = query("""
        SELECT DISTINCT ON (r.ticker) r.ticker, r.action, r.confidence, r.reasoning,
               COALESCE(u.company_name, r.ticker) AS company_name, u.sector
        FROM recommendations r
        LEFT JOIN universe u ON r.ticker = u.ticker
        WHERE r.signal_sources LIKE '%REBALANCE%'
          AND r.date >= CURRENT_DATE - INTERVAL '1 day'
          AND r.action != 'HOLD'
        ORDER BY r.ticker, r.confidence DESC
    """)

    import json as _json
    from portfolio.sector_utils import ALL_REGIONS, DEFAULT_REGION_TARGETS
    region_row = query("SELECT value FROM user_settings WHERE key='region_targets'")
    region_targets = _json.loads(region_row[0]['value']) if region_row and region_row[0]['value'] else DEFAULT_REGION_TARGETS.copy()
    current_region = _get_current_region_exposure()

    return render_template_string(
        make_page(_EXPOSURE, 'Exposure'),
        current_page='Allocation',
        targets=targets, current=current,
        all_sectors=ALL_SECTORS, cooldown_days=cooldown_days,
        rebalance_recs=rebalance_recs,
        total_target=sum(targets.values()),
        region_targets=region_targets, current_region=current_region,
        all_regions=ALL_REGIONS, total_region_target=sum(region_targets.values()),
        health=get_system_health(),
    )


@app.route('/exposure/save', methods=['POST'])
def exposure_save():
    import json as _json
    new_targets = {}
    for s in ALL_SECTORS:
        val = request.form.get(f'sector_{s}', '0')
        try:
            new_targets[s] = max(0, min(100, int(val)))
        except ValueError:
            new_targets[s] = 0

    total = sum(new_targets.values())
    if total > 100:
        flash(f'Sector targets sum to {total}% — must be ≤ 100%. Adjust and save again.')
        return redirect(url_for('exposure'))

    execute("""
        INSERT INTO user_settings (key, value)
        VALUES ('sector_targets', %s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
    """, (_json.dumps(new_targets),))

    # Save region targets
    from portfolio.sector_utils import ALL_REGIONS
    new_region_targets = {}
    for r in ALL_REGIONS:
        val = request.form.get(f'region_{r}', '0')
        try:
            new_region_targets[r] = max(0, min(100, int(val)))
        except ValueError:
            new_region_targets[r] = 0
    region_total = sum(new_region_targets.values())
    if region_total > 100:
        flash(f'Region targets sum to {region_total}% — must be ≤ 100%.')
        return redirect(url_for('exposure'))
    execute("""
        INSERT INTO user_settings (key, value)
        VALUES ('region_targets', %s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
    """, (_json.dumps(new_region_targets),))

    cooldown = request.form.get('cooldown_days', '5')
    execute("""
        INSERT INTO user_settings (key, value)
        VALUES ('ticker_cooldown_days', %s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
    """, (cooldown,))

    log('INFO', 'dashboard', f'Sector targets updated: {new_targets} | Region targets: {new_region_targets}')

    if request.form.get('trigger_rebalance') == '1':
        # Run rebalance analysis in background
        import threading as _th
        def _rebalance():
            try:
                from analysis.trading_agents_wrapper import analyze_candidates
                from db.database import query as bq, execute as bex, log as blog
                import json as _j
                held = {r['ticker'] for r in bq("SELECT ticker FROM holdings WHERE active=TRUE")}
                today = date.today()
                rows = bq("""
                    SELECT * FROM (
                        SELECT DISTINCT ON (dc.ticker)
                               dc.ticker, dc.direction, dc.event_type,
                               dc.total_score, dc.blended_score,
                               dc.gdelt_score, dc.quant_score, dc.reason,
                               dc.eligible, dc.tax_notes, dc.currency, dc.fx_risk,
                               COALESCE(u.sector, '') AS sector
                        FROM discovery_candidates dc
                        LEFT JOIN universe u ON dc.ticker = u.ticker
                        WHERE dc.date = %s AND dc.eligible = TRUE
                        ORDER BY dc.ticker, dc.total_score DESC
                    ) sub
                    WHERE NOT (direction = 'SELL' AND ticker != ALL(%s))
                    ORDER BY COALESCE(blended_score, total_score) DESC
                    LIMIT 30
                """, (today, list(held) or ['']))
                # Tag as rebalance
                for r in rows:
                    r['signal_sources'] = 'REBALANCE,' + r.get('event_type', '')
                recs = analyze_candidates(rows)
                blog('INFO', 'dashboard', f'Rebalance analysis complete: {len(recs)} recs')
            except Exception as e:
                from db.database import log as blog
                blog('ERROR', 'dashboard', f'Rebalance error: {e}')
        _th.Thread(target=_rebalance, daemon=True).start()
        flash('Targets saved. Rebalance analysis running in background — refresh in ~10 min.')
    else:
        flash('Sector targets saved.')

    return redirect(url_for('exposure'))


_EXPOSURE = """
<div class="d-flex justify-content-between align-items-center mb-3">
  <span class="page-header mb-0">Sector Exposure Targets</span>
  <small style="color:var(--muted);font-size:.78rem">
    Controls which sectors get priority slots in daily TradingAgents analysis
  </small>
</div>

{% if rebalance_recs %}
<div class="card mb-3" style="border-left:3px solid var(--accent)">
  <div class="card-header">Rebalance Suggestions</div>
  <div class="card-body p-0">
    <table class="table table-sm mb-0">
      <thead><tr><th>Ticker</th><th>Sector</th><th>Action</th><th>Confidence</th><th>Reasoning</th></tr></thead>
      <tbody>
      {% for r in rebalance_recs %}
      <tr>
        <td><a href="/ticker/{{ r.ticker }}" style="font-weight:700;color:var(--accent)">{{ r.ticker }}</a>
          <br><small style="color:var(--muted)">{{ r.company_name }}</small></td>
        <td><small style="color:var(--muted)">{{ r.sector or '—' }}</small></td>
        <td><span class="badge badge-{{ r.action|lower }}">{{ r.action }}</span></td>
        <td><strong>{{ (r.confidence|float*100)|int }}%</strong></td>
        <td style="font-size:.78rem;color:var(--muted)">{{ (r.reasoning or '')[:120] }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endif %}

<form method="POST" action="/exposure/save">
<div class="row g-3 mb-3">
  <div class="col-md-8">
    <div class="card h-100">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span>Target Allocation</span>
        <span id="totalPct" style="font-size:.82rem;font-weight:700;color:var(--green)">
          {{ total_target }}% allocated
        </span>
      </div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <thead><tr>
            <th>Sector</th>
            <th style="width:130px">Current</th>
            <th style="width:160px">Target %</th>
            <th style="width:80px">Gap</th>
          </tr></thead>
          <tbody>
          {% for sector in all_sectors %}
          {% set cur = current.get(sector, 0) %}
          {% set tgt = targets.get(sector, 0) %}
          {% set gap = cur - tgt %}
          <tr>
            <td style="font-weight:500">{{ sector }}</td>
            <td>
              {% if cur > 0 %}
              <div class="progress" style="height:14px;background:var(--surface2)">
                <div class="progress-bar" style="width:{{ [cur,100]|min }}%;background:var(--accent);font-size:.65rem">
                  {{ '%.1f'|format(cur) }}%
                </div>
              </div>
              {% else %}
              <span style="color:var(--muted);font-size:.78rem">—</span>
              {% endif %}
            </td>
            <td>
              <input type="number" name="sector_{{ sector }}"
                     value="{{ tgt }}" min="0" max="100" step="1"
                     class="form-control form-control-sm sector-input"
                     style="width:90px;display:inline-block"
                     oninput="updateTotal()">
            </td>
            <td style="font-size:.82rem;font-weight:600;color:{% if gap > 5 %}var(--red){% elif gap < -5 %}var(--green){% else %}var(--muted){% endif %}">
              {% if cur > 0 %}
                {{ '%+.1f'|format(gap) }}%
              {% else %}—{% endif %}
            </td>
          </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="col-md-4">
    <div class="card mb-3">
      <div class="card-header">Analysis Settings</div>
      <div class="card-body">
        <label style="font-size:.82rem;color:var(--muted)">Ticker Cooldown (days)</label>
        <input type="number" name="cooldown_days" value="{{ cooldown_days }}"
               min="0" max="30" class="form-control form-control-sm mb-3"
               title="Skip re-analyzing a ticker for this many days after last recommendation">
        <p style="font-size:.75rem;color:var(--muted)">
          Prevents the same large-cap tickers dominating every day.
          Set to 0 to disable cooldown.
        </p>
        <div class="form-check mb-3">
          <input class="form-check-input" type="checkbox" name="trigger_rebalance" value="1" id="triggerChk">
          <label class="form-check-label" style="font-size:.82rem" for="triggerChk">
            Trigger rebalance analysis after saving
          </label>
        </div>
        <p style="font-size:.75rem;color:var(--muted)">
          Runs TradingAgents analysis respecting the new sector targets
          and surfaces BUY/SELL suggestions. Takes ~10 min.
        </p>
      </div>
    </div>

    <div class="card">
      <div class="card-header">Remaining / Cash</div>
      <div class="card-body">
        <div id="cashSlice" style="font-size:1.4rem;font-weight:700;color:var(--green)">
          {{ 100 - total_target }}%
        </div>
        <div style="font-size:.78rem;color:var(--muted)">unallocated (kept as cash buffer)</div>
        <div id="overAllocWarn" style="display:none;color:var(--red);font-size:.78rem;margin-top:.5rem">
          ⚠ Total exceeds 100% — reduce targets before saving
        </div>
      </div>
    </div>
  </div>
</div>

<hr class="my-3" style="border-color:var(--surface2)">

<div class="card mb-3">
  <div class="card-header d-flex justify-content-between align-items-center">
    <span>Geographic Targets (by Region)</span>
    <span id="regionTotalPct" style="font-size:.82rem;font-weight:700;color:var(--green)">
      {{ total_region_target }}% allocated
    </span>
  </div>
  <div class="card-body p-0">
    <table class="table table-sm mb-0">
      <thead><tr>
        <th>Region</th>
        <th style="width:130px">Current</th>
        <th style="width:160px">Target %</th>
        <th style="width:80px">Gap</th>
      </tr></thead>
      <tbody>
      {% for region in all_regions %}
      {% set cur = current_region.get(region, 0) %}
      {% set tgt = region_targets.get(region, 0) %}
      {% set gap = cur - tgt %}
      <tr>
        <td style="font-weight:500">{{ region }}</td>
        <td>
          {% if cur > 0 %}
          <div class="progress" style="height:14px;background:var(--surface2)">
            <div class="progress-bar" style="width:{{ [cur,100]|min }}%;background:var(--blue,#4a9eff);font-size:.65rem">
              {{ '%.1f'|format(cur) }}%
            </div>
          </div>
          {% else %}
          <span style="color:var(--muted);font-size:.78rem">—</span>
          {% endif %}
        </td>
        <td>
          <input type="number" name="region_{{ region }}"
                 value="{{ tgt }}" min="0" max="100" step="5"
                 class="form-control form-control-sm region-input"
                 style="width:90px;display:inline-block"
                 oninput="updateRegionTotal()">
        </td>
        <td style="font-size:.82rem;font-weight:600;color:{% if gap > 10 %}var(--red){% elif gap < -10 %}var(--green){% else %}var(--muted){% endif %}">
          {% if cur > 0 %}
            {{ '%+.1f'|format(gap) }}%
          {% else %}—{% endif %}
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<div class="d-flex gap-2">
  <button type="submit" class="btn btn-success px-4">&#128190; Save Targets</button>
  <button type="submit" onclick="document.getElementById('triggerChk').checked=true"
          class="btn btn-outline-primary px-4">&#9861; Save &amp; Rebalance Now</button>
</div>
</form>

<script>
function updateTotal() {
  var inputs = document.querySelectorAll('.sector-input');
  var total = 0;
  inputs.forEach(function(i) { total += parseInt(i.value || 0); });
  var el = document.getElementById('totalPct');
  var cash = document.getElementById('cashSlice');
  var warn = document.getElementById('overAllocWarn');
  el.textContent = total + '% allocated';
  el.style.color = total > 100 ? 'var(--red)' : total >= 80 ? 'var(--amber)' : 'var(--green)';
  cash.textContent = Math.max(0, 100 - total) + '%';
  warn.style.display = total > 100 ? 'block' : 'none';
}
function updateRegionTotal() {
  var inputs = document.querySelectorAll('.region-input');
  var total = 0;
  inputs.forEach(function(i) { total += parseInt(i.value || 0); });
  var el = document.getElementById('regionTotalPct');
  el.textContent = total + '% allocated';
  el.style.color = total > 100 ? 'var(--red)' : total >= 80 ? 'var(--amber)' : 'var(--green)';
}
</script>
"""


@app.route('/watchlist')
def watchlist_view():
    wl = query("""
        SELECT w.ticker, w.company_name, u.sector,
               ns.avg_tone
        FROM watchlist w
        LEFT JOIN universe u ON w.ticker = u.ticker
        LEFT JOIN LATERAL (
            SELECT avg_tone FROM news_sentiment
            WHERE ticker = w.ticker ORDER BY date DESC LIMIT 1
        ) ns ON TRUE
        WHERE w.active = TRUE ORDER BY w.ticker
    """)

    uni_row = query("SELECT COUNT(*) AS cnt FROM universe WHERE active=TRUE")
    uni_count = int(uni_row[0]['cnt']) if uni_row else 0

    upd = query("SELECT MAX(updated_at) AS ts FROM universe")
    uni_updated = upd[0]['ts'].strftime('%Y-%m-%d') if upd and upd[0]['ts'] else None

    return render_template_string(
        make_page(_WATCHLIST, 'Watchlist'),
        current_page='Watchlist',

        watchlist=wl, uni_count=uni_count, uni_updated=uni_updated,
        health=get_system_health(),
    )


@app.route('/watchlist/add', methods=['POST'])
def watchlist_add():
    ticker = request.form.get('ticker', '').upper().strip()
    company = request.form.get('company_name', '').strip()
    if ticker:
        execute("""
            INSERT INTO watchlist (ticker, company_name, active) VALUES (%s, %s, TRUE)
            ON CONFLICT (ticker) DO UPDATE SET active=TRUE, company_name=EXCLUDED.company_name
        """, (ticker, company or ticker))
        log('INFO', 'dashboard', f'Added {ticker} to watchlist')
        flash(f'Added {ticker} to watchlist.')
    return redirect(url_for('watchlist_view'))


@app.route('/watchlist/remove', methods=['POST'])
def watchlist_remove():
    ticker = request.form.get('ticker', '').upper().strip()
    if ticker:
        execute("UPDATE watchlist SET active=FALSE WHERE ticker=%s", (ticker,))
        log('INFO', 'dashboard', f'Removed {ticker} from watchlist')
        flash(f'Removed {ticker} from watchlist.')
    return redirect(url_for('watchlist_view'))


@app.route('/api/analysis-logs/<ticker>')
def api_analysis_logs(ticker):
    import os as _os, json as _json, time as _time
    ticker = ticker.upper().strip()
    logs = query("""
        SELECT level, message, created_at FROM system_logs
        WHERE component='trading_agents'
          AND message LIKE %s
          AND created_at >= NOW() - INTERVAL '24 hours'
        ORDER BY created_at DESC LIMIT 40
    """, (f'%{ticker}%',))

    with _analysis_lock:
        active      = _analysis_state['active']
        waiting     = list(_analysis_state['queue'])
        debate_data = dict(_analysis_state['debate'].get(ticker, {}))

    # Also read file-based state written by pipeline process
    file_state = {}
    debate_file = f'/tmp/advisor_debate_{ticker}.json'
    if _os.path.exists(debate_file):
        try:
            with open(debate_file) as f:
                file_state = _json.load(f)
            # Use file state if it's fresher and has more steps, or in-memory is empty
            file_age = _time.time() - file_state.get('updated', 0)
            file_steps = file_state.get('steps_done', [])
            mem_steps  = debate_data.get('steps_done', [])
            if file_age < 600 and len(file_steps) >= len(mem_steps):
                debate_data = {
                    'step_active': file_state.get('step_active', 0),
                    'steps_done':  file_steps,
                }
        except Exception:
            pass

    # Check if pipeline is currently analyzing this ticker
    # A single step can take up to ~5 min, so use 360s window
    pipeline_analyzing = False
    if not (active == ticker):
        file_age = _time.time() - file_state.get('updated', 0)
        if file_age < 360 and file_state.get('status') == 'running':
            pipeline_analyzing = True

    is_active = (active == ticker) or pipeline_analyzing
    queue_pos = (waiting.index(ticker) + 1) if ticker in waiting else 0
    in_queue  = is_active or queue_pos > 0
    running   = is_active

    return jsonify({
        'logs': [{'time': r['created_at'].strftime('%H:%M:%S'),
                  'level': r['level'],
                  'message': r['message']} for r in reversed(logs)],
        'running':     running,
        'in_queue':    in_queue,
        'queue_pos':   queue_pos,
        'active':      active,
        'queue_list':  waiting,
        'debate':      debate_data,
        'debate_steps': [{'num': n, 'name': nm, 'desc': d} for n, nm, d in DEBATE_STEPS],
    })


PIPELINE_STAGES_DEF = [
    ('collect_all',        'Data Collection'),
    ('event_classifier',   'Event Classifier'),
    ('event_mapper',       'Event Mapper'),
    ('scorer',             'Candidate Scorer'),
    ('tax_filter',         'Finnish Tax Filter'),
    ('trading_agents',     'TradingAgents Analysis'),
    ('holdings_monitor',   'Holdings Monitor'),
    ('portfolio_brain',    'Portfolio Brain'),
    ('sanity_check',       'Sanity Check'),
    ('performance_tracker','Performance Tracker'),
]

@app.route('/api/pipeline-stage')
def api_pipeline_stage():
    """Return current pipeline stage from /tmp/advisor_pipeline_stage.json."""
    import json as _json, os as _os
    stage_file = '/tmp/advisor_pipeline_stage.json'
    current = {'stage': 'idle', 'label': 'Idle', 'status': 'idle', 'ts': '', 'pid': 0}
    if _os.path.exists(stage_file):
        try:
            with open(stage_file) as f:
                current = _json.load(f)
        except Exception:
            pass
    running = _pipeline_is_running()
    # If file says running but process is gone, it crashed
    if current.get('status') == 'running' and not running:
        current['status'] = 'crashed'

    # Add TradingAgents analysis progress when that stage is active
    ta_progress = None
    if current.get('stage') == 'trading_agents' or _pipeline_is_running():
        import json as _json2, time as _t2
        try:
            prog_file = '/tmp/advisor_ta_progress.json'
            if os.path.exists(prog_file):
                age = _t2.time() - os.path.getmtime(prog_file)
                if age < 7200:  # within 2 hours
                    p = _json2.loads(open(prog_file).read())
                    total    = int(p.get('total', 16))
                    done     = int(p.get('done', 0))
                    current_ = p.get('current')
                    ta_progress = {
                        'completed':   done,
                        'expected':    total,
                        'in_progress': current_,
                        'pct':         int(done / total * 100) if total else 0,
                    }
            if ta_progress is None:
                # Fallback: count from recommendations table
                recs_today = query("""
                    SELECT COUNT(DISTINCT ticker) AS cnt FROM recommendations
                    WHERE date=CURRENT_DATE AND signal_sources='trading_agents'
                """)
                rec_count = int(recs_today[0]['cnt']) if recs_today else 0
                ta_progress = {
                    'completed': rec_count, 'expected': 16,
                    'in_progress': None, 'pct': int(rec_count / 16 * 100),
                }
        except Exception:
            pass

    return jsonify({
        'current':     current,
        'running':     running,
        'stages':      [{'key': k, 'label': l} for k, l in PIPELINE_STAGES_DEF],
        'ta_progress': ta_progress,
    })


@app.route('/api/ollama-status')  # keep old name for any cached browser requests
@app.route('/api/analysis-status')
def api_ollama_status():
    """Return analysis busy/idle status (Groq + OpenRouter)."""
    with _analysis_lock:
        active  = _analysis_state['active']
        waiting = list(_analysis_state['queue'])
    busy = bool(active)
    pipeline_busy = _pipeline_is_running() and busy
    return jsonify({
        'busy':          busy,
        'model':         'groq+openrouter',
        'active_ticker': active,
        'queue':         waiting,
        'pipeline_busy': pipeline_busy,
    })


@app.route('/api/rate-limit-status')
def api_rate_limit_status():
    """Return current rate limit / budget status for Groq and OpenRouter."""
    try:
        from analysis.rate_limit_manager import get_manager
        s = get_manager().status()
        return jsonify({'ok': True, **s})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/brief')
def api_brief():
    from portfolio.portfolio_brain import build_brief
    brief = build_brief()
    brief['date'] = str(brief['date'])
    return jsonify(brief)


@app.route('/health')
def health():
    """Lightweight liveness probe — used by systemd and external monitors."""
    try:
        query("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    worker_ok = _analysis_worker_thread.is_alive()
    status = 200 if (db_ok and worker_ok) else 503
    return jsonify({
        'ok':        db_ok and worker_ok,
        'db':        db_ok,
        'worker':    worker_ok,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
    }), status


@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    """Catch-all: log and return 500 without crashing the process."""
    log('ERROR', 'dashboard', f'Unhandled exception in route: {type(e).__name__}: {e}')
    return jsonify({'error': 'Internal server error', 'detail': str(e)}), 500


@app.errorhandler(404)
def handle_404(e):
    return jsonify({'error': 'Not found'}), 404


# ── Ticker detail page ────────────────────────────────────────────────────────

_TICKER_DETAIL = """
{# ── Breadcrumb back navigation ── #}
<nav style="margin-bottom:.75rem;font-size:.8rem;color:var(--sub)">
  <a href="javascript:history.back()" style="color:var(--accent);text-decoration:none;display:inline-flex;align-items:center;gap:.3rem">
    &#8592; Back
  </a>
  <span style="margin:0 .4rem;opacity:.4">/</span>
  <a href="/" style="color:var(--sub);text-decoration:none">Portfolio</a>
  <span style="margin:0 .4rem;opacity:.4">/</span>
  <span style="color:var(--text);font-weight:600">{{ ticker }}</span>
</nav>

{# ── Ticker header bar ── #}
<div class="d-flex align-items-start gap-3 mb-3 flex-wrap">
  <div>
    <div class="d-flex align-items-center gap-2 mb-1">
      <h4 style="margin:0;color:var(--text);font-size:1.5rem;font-weight:700;letter-spacing:-.4px">{{ ticker }}</h4>
      {% if in_holdings %}
      <span style="font-size:.7rem;padding:.2rem .55rem;border-radius:999px;background:rgba(79,142,247,.12);color:var(--accent);border:1px solid rgba(79,142,247,.25);font-weight:600">HELD</span>
      {% endif %}
      {% if in_watchlist %}
      <span style="font-size:.7rem;padding:.2rem .55rem;border-radius:999px;background:rgba(245,158,11,.1);color:var(--amber);border:1px solid rgba(245,158,11,.2);font-weight:500">WATCHING</span>
      {% endif %}
    </div>
    <div style="color:var(--sub);font-size:.88rem;margin-bottom:.3rem">{{ company }}</div>
    <div style="display:flex;gap:.4rem;flex-wrap:wrap">
      {% if meta.sector %}<span class="badge bg-secondary" style="font-size:.68rem;font-weight:400">{{ meta.sector }}</span>{% endif %}
      {% if meta.country %}<span class="badge bg-secondary" style="font-size:.68rem;font-weight:400">{{ meta.country }}</span>{% endif %}
    </div>
  </div>
  <div class="ms-auto d-flex gap-2 flex-wrap align-items-start">
    <button class="btn btn-success btn-sm px-3" data-bs-toggle="modal" data-bs-target="#tradeModal"
            onclick="openTrade('BUY','{{ ticker }}',{{ rec_shares }},{{ rec_price }})">BUY</button>
    {% if in_holdings %}
    <button class="btn btn-danger btn-sm px-3" data-bs-toggle="modal" data-bs-target="#tradeModal"
            onclick="openTrade('SELL','{{ ticker }}',{{ holding.shares if holding else 0 }},{{ rec_price }})">SELL</button>
    {% else %}
    <button class="btn btn-outline-danger btn-sm px-3" disabled title="Not in holdings">SELL</button>
    {% endif %}
    {% if in_watchlist %}
    <form method="post" action="/watchlist/remove" style="display:inline">
      <input type="hidden" name="ticker" value="{{ ticker }}">
      <button class="btn btn-outline-warning btn-sm px-3">Unwatch</button>
    </form>
    {% else %}
    <form method="post" action="/watchlist/add" style="display:inline">
      <input type="hidden" name="ticker" value="{{ ticker }}">
      <input type="hidden" name="company_name" value="{{ company }}">
      <button class="btn btn-outline-warning btn-sm px-3">Watch</button>
    </form>
    {% endif %}
    <form method="post" action="/analyze/{{ ticker }}" id="analyzeNowForm" style="display:inline">
      <button type="submit" id="analyzeNowBtn" class="btn btn-outline-secondary btn-sm px-3 analyze-disabled-when-busy"
              title="On-demand: uses Groq + OpenRouter (2 OR calls from your 5/day quota)">&#9881; Analyze Now</button>
    </form>
    <span id="ondemandQuota" style="font-size:.75rem;color:var(--sub);margin-left:.4rem"></span>
  </div>
</div>

{# ── Trade modal ── #}
<div class="modal fade" id="tradeModal" tabindex="-1">
  <div class="modal-dialog modal-sm">
    <div class="modal-content">
      <div class="modal-header">
        <h6 class="modal-title" id="tradeModalTitle">Log Trade</h6>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <form method="POST" action="/trade">
        <div class="modal-body">
          <input type="hidden" name="ticker" id="modalTicker">
          <input type="hidden" name="action" id="modalAction">
          <div class="mb-2">
            <label class="form-label" style="font-size:.8rem">Shares</label>
            <input type="number" name="shares" id="modalShares" class="form-control form-control-sm" step="1" min="1" required>
          </div>
          <div class="mb-2">
            <label class="form-label" style="font-size:.8rem">Price (local currency)</label>
            <input type="number" name="price" id="modalPrice" class="form-control form-control-sm" step="0.01" min="0.01" required>
          </div>
          <div class="mb-2">
            <label class="form-label" style="font-size:.8rem">Currency</label>
            <input type="text" name="currency" id="modalCurrency" class="form-control form-control-sm" placeholder="USD">
          </div>
          <div class="mb-2">
            <label class="form-label" style="font-size:.8rem">Date</label>
            <input type="date" name="trade_date" class="form-control form-control-sm" value="{{ today }}">
          </div>
          <div class="mb-2">
            <label class="form-label" style="font-size:.8rem">Notes</label>
            <input type="text" name="notes" class="form-control form-control-sm" placeholder="Optional">
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="submit" class="btn btn-sm btn-success" id="modalSubmit">Log Trade</button>
        </div>
      </form>
    </div>
  </div>
</div>
<script>
function openTrade(action, ticker, shares, price) {
  document.getElementById('modalTicker').value = ticker;
  document.getElementById('modalAction').value = action;
  document.getElementById('modalShares').value = shares ? Math.max(1, Math.round(shares)) : '';
  document.getElementById('modalPrice').value = price || '';
  document.getElementById('tradeModalTitle').textContent = action + ' ' + ticker;
  var btn = document.getElementById('modalSubmit');
  btn.className = action==='SELL' ? 'btn btn-sm btn-danger' : 'btn btn-sm btn-success';
  btn.textContent = 'Log ' + action;
}
</script>

{# ── Hero: price + P&L strip ── #}
<div class="d-flex gap-3 mb-3 flex-wrap" style="font-size:.88rem">
  <div style="color:var(--sub)">Price <strong style="color:var(--text);font-size:1.05rem">{{ price_cur }}</strong></div>
  {% if pnl_pct != 0 %}
  <div style="color:var(--sub)">vs rec
    <strong style="color:{% if pnl_pct >= 0 %}var(--green){% else %}var(--red){% endif %}">
      {{ '+' if pnl_pct >= 0 else '' }}{{ '%.1f'|format(pnl_pct) }}%
    </strong>
  </div>
  {% endif %}
  {% if velocity != '—' %}
  <div style="color:var(--sub)">GDELT velocity <strong style="color:var(--amber)">{{ velocity }}x</strong></div>
  {% endif %}
  {% if in_holdings and holding %}
  <div style="color:var(--sub)">P&amp;L
    <strong style="color:{% if pnl_eur >= 0 %}var(--green){% else %}var(--red){% endif %}">
      {{ '+' if pnl_eur >= 0 else '' }}&#8364;{{ '%.0f'|format(pnl_eur) }}
    </strong>
    ({{ holding.shares }} shares @ {{ holding.currency }} {{ '%.2f'|format(holding.avg_buy_price|float) }})
  </div>
  {% endif %}
</div>

{# ── Hero: recommendation decision card ── #}
{% if latest_rec %}
{% set conv_class = 'high' if latest_rec.confidence|float >= 0.75 else ('medium' if latest_rec.confidence|float >= 0.5 else 'low') %}
{% set conv_label = 'High conviction' if latest_rec.confidence|float >= 0.75 else ('Medium conviction' if latest_rec.confidence|float >= 0.5 else 'Low conviction') %}
<div class="decision-card {{ latest_rec.action|lower }} mb-3">
  <div class="d-flex justify-content-between align-items-start mb-2">
    <div>
      <span class="decision-action {{ latest_rec.action|lower }}">{{ latest_rec.action }}</span>
      <span class="conviction {{ conv_class }} ms-2">{{ conv_label }} — {{ (latest_rec.confidence|float*100)|int }}%</span>
      {% if latest_rec.time_horizon %}
      <span class="badge bg-secondary ms-1" style="font-size:.68rem;font-weight:400">{{ latest_rec.time_horizon }}</span>
      {% endif %}
    </div>
    <span style="font-size:.78rem;color:var(--sub)">{{ latest_rec.date }}</span>
  </div>

  {# Clean natural language first #}
  <p class="decision-body" style="margin-bottom:.75rem">{{ natural_summary }}</p>

  {# Bull / Bear / Risks if available #}
  {% if latest_rec.bull_case %}
  <div style="background:rgba(34,197,94,.07);border:1px solid rgba(34,197,94,.2);border-radius:6px;padding:.55rem .8rem;margin-bottom:.4rem">
    <div style="font-size:.68rem;font-weight:700;color:var(--green);margin-bottom:.2rem;text-transform:uppercase;letter-spacing:.4px">Bull case</div>
    <div style="font-size:.8rem;color:var(--text);line-height:1.55">{{ latest_rec.bull_case }}</div>
  </div>
  {% endif %}
  {% if latest_rec.bear_case %}
  <div style="background:rgba(244,63,94,.07);border:1px solid rgba(244,63,94,.2);border-radius:6px;padding:.55rem .8rem;margin-bottom:.4rem">
    <div style="font-size:.68rem;font-weight:700;color:var(--red);margin-bottom:.2rem;text-transform:uppercase;letter-spacing:.4px">Bear case</div>
    <div style="font-size:.8rem;color:var(--text);line-height:1.55">{{ latest_rec.bear_case }}</div>
  </div>
  {% endif %}
  {% if latest_rec.key_risks %}
  <div style="background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.2);border-radius:6px;padding:.55rem .8rem;margin-bottom:.4rem">
    <div style="font-size:.68rem;font-weight:700;color:var(--amber);margin-bottom:.2rem;text-transform:uppercase;letter-spacing:.4px">Key risks</div>
    <div style="font-size:.8rem;color:var(--text);line-height:1.55">{{ latest_rec.key_risks }}</div>
  </div>
  {% endif %}

  {# Sources footer #}
  {% if latest_rec.signal_sources %}
  <div style="margin-top:.6rem;padding-top:.5rem;border-top:1px solid rgba(255,255,255,.06);font-size:.72rem;color:var(--sub)">
    {% for src in latest_rec.signal_sources.split(',') if src.strip() %}
    <span class="signal-pill on" style="font-size:.68rem">{{ src.strip() }}</span>
    {% endfor %}
  </div>
  {% endif %}

  {# Full reasoning (collapsed) #}
  <div style="margin-top:.5rem">
    <button class="btn btn-sm" style="font-size:.72rem;color:var(--sub);padding:.2rem .4rem;background:none;border:none"
            data-bs-toggle="collapse" data-bs-target="#fullReasoning">
      Full reasoning &#8964;
    </button>
    <div id="fullReasoning" class="collapse">
      <div style="margin-top:.5rem;font-size:.78rem;color:var(--sub);line-height:1.65;padding:.5rem .6rem;background:var(--surface2);border-radius:6px">
        {{ latest_rec.reasoning or '—' }}
      </div>
      {% if latest_rec.debate_trace %}
      <button class="btn btn-sm btn-outline-secondary mt-2" style="font-size:.7rem"
              data-bs-toggle="collapse" data-bs-target="#debateTrace">
        Show full agent debate trace
      </button>
      <div id="debateTrace" class="collapse mt-2">
        <pre style="font-size:.7rem;color:var(--sub);background:var(--surface2);border-radius:6px;
                    padding:.75rem;max-height:400px;overflow-y:auto;white-space:pre-wrap">{{ latest_rec.debate_trace }}</pre>
      </div>
      {% endif %}
    </div>
  </div>
</div>
{% else %}
<div class="card mb-3">
  <div class="card-body" style="color:var(--sub);text-align:center;padding:2rem 1rem">
    <div style="font-size:1.8rem;opacity:.3;margin-bottom:.5rem">📊</div>
    No recommendation yet. Click <strong>Analyze Now</strong> to run the 9-agent AI debate (Groq + OpenRouter).
  </div>
</div>
{% endif %}

{# ── Queue / analysis status banner ── #}
<div id="queueBanner" style="display:none" class="mb-3"></div>

{# ── Tabs for data exploration ── #}
<ul class="nav nav-tabs mb-3" id="tickerTabs">
  <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#tabDebate">Agent Debate</a></li>
  <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tabQuant">Risk &amp; Return</a></li>
  <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tabFundamentals">Fundamentals</a></li>
  <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tabGdelt">GDELT &amp; News</a></li>
  <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#tabRaw">Raw Data</a></li>
</ul>

<div class="tab-content">

{# ── TAB: Agent Debate ── #}
<div class="tab-pane fade show active" id="tabDebate">

{% if debate_trace %}
{# ── Saved debate — full structured flow ── #}
<div style="position:relative;padding-left:2px">

  {# Step 1+2: Analysts side-by-side #}
  <div class="row g-2 mb-2">
    <div class="col-12 col-md-6">
      <div class="card h-100" style="border-left:3px solid var(--accent)">
        <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:var(--accent)">
          ① MARKET ANALYST <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— price action &amp; momentum</span>
        </div>
        <div class="card-body" style="font-size:.83rem;line-height:1.6;color:var(--text)">{{ debate_trace.market_analyst or '—' }}</div>
      </div>
    </div>
    <div class="col-12 col-md-6">
      <div class="card h-100" style="border-left:3px solid var(--accent)">
        <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:var(--accent)">
          ② NEWS ANALYST <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— sentiment &amp; catalysts</span>
        </div>
        <div class="card-body" style="font-size:.83rem;line-height:1.6;color:var(--text)">{{ debate_trace.news_analyst or '—' }}</div>
      </div>
    </div>
  </div>

  {# Debate rounds #}
  {% for rd in (debate_trace.investment_debate or debate_trace.debate or []) %}
  <div class="row g-2 mb-2">
    <div class="col-12 col-md-6">
      <div class="card h-100" style="border-left:3px solid var(--red)">
        <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:var(--red)">
          🐻 BEAR — ROUND {{ rd.round }}
        </div>
        <div class="card-body" style="font-size:.83rem;line-height:1.6;color:var(--text)">{{ rd.bear or '—' }}</div>
      </div>
    </div>
    <div class="col-12 col-md-6">
      <div class="card h-100" style="border-left:3px solid var(--green)">
        <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:var(--green)">
          🐂 BULL — ROUND {{ rd.round }}
        </div>
        <div class="card-body" style="font-size:.83rem;line-height:1.6;color:var(--text)">{{ rd.bull or '—' }}</div>
      </div>
    </div>
  </div>
  {% endfor %}

  {# Evaluator synthesis #}
  <div class="card mb-2" style="border-left:3px solid #a78bfa">
    <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:#a78bfa">
      ③ RESEARCH EVALUATOR (Groq) <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— synthesises the debate into a verdict</span>
    </div>
    <div class="card-body" style="font-size:.83rem;line-height:1.6;color:var(--text)">{{ debate_trace.evaluator or '—' }}</div>
  </div>

  {# Trader #}
  <div class="card mb-2" style="border-left:3px solid var(--amber)">
    <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:var(--amber)">
      ④ TRADER SIGNAL (Groq) <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— preliminary BUY/SELL/HOLD/WATCH call</span>
    </div>
    <div class="card-body" style="font-size:.83rem;line-height:1.6;color:var(--text)">{{ debate_trace.trader or '—' }}</div>
  </div>

  {# ── Risk debate: 3-way Groq (new) OR single analyst (legacy) ── #}
  {% if debate_trace.risk_debate %}
  <div class="card mb-2" style="border-left:3px solid #f97316">
    <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:#f97316">
      ⑤ RISK DEBATE (Groq ×3) <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— aggressive · conservative · neutral</span>
    </div>
    <div class="card-body">
      <div class="row g-2">
        <div class="col-12 col-md-4">
          <div style="font-size:.7rem;font-weight:700;color:#22c55e;text-transform:uppercase;letter-spacing:.4px;margin-bottom:.3rem">▲ Aggressive</div>
          <div style="font-size:.82rem;line-height:1.55;color:var(--text)">{{ debate_trace.risk_debate.aggressive or '—' }}</div>
        </div>
        <div class="col-12 col-md-4">
          <div style="font-size:.7rem;font-weight:700;color:#ef4444;text-transform:uppercase;letter-spacing:.4px;margin-bottom:.3rem">▼ Conservative</div>
          <div style="font-size:.82rem;line-height:1.55;color:var(--text)">{{ debate_trace.risk_debate.conservative or '—' }}</div>
        </div>
        <div class="col-12 col-md-4">
          <div style="font-size:.7rem;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.4px;margin-bottom:.3rem">◆ Neutral</div>
          <div style="font-size:.82rem;line-height:1.55;color:var(--text)">{{ debate_trace.risk_debate.neutral or '—' }}</div>
        </div>
      </div>
    </div>
  </div>

  {# Risk Manager (OR) — synthesises debate + challenges PM #}
  <div class="card mb-2" style="border-left:3px solid var(--red)">
    <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:var(--red)">
      ⑥ RISK MANAGER (OpenRouter/Nvidia) <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— synthesis + challenges to PM</span>
    </div>
    <div class="card-body" style="font-size:.83rem;line-height:1.6;color:var(--text)">{{ debate_trace.risk_manager or '—' }}</div>
  </div>

  {# PM Response (OpenRouter) — counters Risk Manager's challenges #}
  {% if debate_trace.pm_response or debate_trace.pm_draft %}
  <div class="card mb-2" style="border-left:3px solid #a78bfa">
    <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:#a78bfa">
      ⑦ PM RESPONSE (OpenRouter/Nvidia) <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— counters Risk Manager's challenges</span>
    </div>
    <div class="card-body" style="font-size:.83rem;line-height:1.6;color:var(--text)">{{ debate_trace.pm_response or debate_trace.pm_draft or '—' }}</div>
  </div>
  {% endif %}

  {# RM Rebuttal (OpenRouter) — Risk Manager's final position after PM counter #}
  {% if debate_trace.risk_manager_rebuttal %}
  <div class="card mb-2" style="border-left:3px solid var(--red)">
    <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:var(--red)">
      ⑧ RM REBUTTAL (OpenRouter/Nvidia) <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— Risk Manager's final position after PM counter</span>
    </div>
    <div class="card-body" style="font-size:.83rem;line-height:1.6;color:var(--text)">{{ debate_trace.risk_manager_rebuttal }}</div>
  </div>
  {% endif %}

  {% else %}
  {# Legacy single Risk Analyst card (pre-3-way-debate runs) #}
  <div class="card mb-2" style="border-left:3px solid var(--red)">
    <div class="card-header" style="font-size:.7rem;letter-spacing:.5px;color:var(--red)">
      ⑤ RISK ANALYST (OpenRouter/Nvidia) <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— portfolio fit, concentration risk, downside scenario</span>
    </div>
    <div class="card-body" style="font-size:.83rem;line-height:1.6;color:var(--text)">{{ debate_trace.risk_analyst or '—' }}</div>
  </div>
  {% endif %}

  {# PM Final Decision #}
  {% set pm_step = '⑨' if debate_trace.risk_debate else '⑥' %}
  {% set pm_color = 'var(--green)' if debate_trace.pm_action in ('BUY','STRONG_BUY') else ('var(--red)' if debate_trace.pm_action == 'SELL' else 'var(--amber)') %}
  <div class="card mb-2" style="border-left:3px solid {{ pm_color }}">
    <div class="card-header d-flex justify-content-between align-items-center" style="font-size:.7rem;letter-spacing:.5px;color:{{ pm_color }}">
      <span>{{ pm_step }} PORTFOLIO MANAGER (OpenRouter/Nvidia) <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— final decision</span></span>
      <span style="font-size:.78rem;font-weight:700">{{ debate_trace.pm_action }}
        <span style="color:var(--sub);font-weight:400">{{ (debate_trace.pm_confidence * 100)|int }}%</span>
      </span>
    </div>
    <div class="card-body">
      <div style="font-size:.83rem;line-height:1.6;color:var(--text);margin-bottom:.5rem">
        <strong style="font-size:.7rem;color:var(--sub);text-transform:uppercase;letter-spacing:.4px">Reasoning</strong><br>
        {{ debate_trace.pm_reasoning or '—' }}
      </div>
      {% if debate_trace.pm_risk_notes %}
      <div style="font-size:.83rem;line-height:1.6;color:var(--text)">
        <strong style="font-size:.7rem;color:var(--amber);text-transform:uppercase;letter-spacing:.4px">Risk notes</strong><br>
        {{ debate_trace.pm_risk_notes }}
      </div>
      {% endif %}
    </div>
  </div>

</div>

{% elif analysis_running %}
{# ── Analysis in progress ── #}
<div class="card">
  <div class="card-header d-flex align-items-center gap-2">
    <span>Agent Debate</span>
    <span class="spinner-border spinner-border-sm" id="debateSpinner" style="color:var(--accent)"></span>
    <small class="ms-auto" style="color:var(--sub);font-size:.72rem" id="logRefreshNote">Analysis running…</small>
  </div>
  <div class="card-body p-3">
    <div id="debateStepBar" class="d-flex flex-wrap gap-2 mb-3"></div>
    <div id="debateOutputs"></div>
  </div>
</div>

{% else %}
{# ── No debate yet ── #}
<div class="card">
  <div class="card-header d-flex align-items-center gap-2">
    <span>Agent Debate</span>
    <span class="spinner-border spinner-border-sm d-none" id="debateSpinner" style="color:var(--accent)"></span>
    <small class="ms-auto" style="color:var(--sub);font-size:.72rem" id="logRefreshNote">Click Analyze Now to run the debate</small>
  </div>
  <div class="card-body p-3" style="text-align:center;padding:2rem!important">
    <div style="font-size:2rem;opacity:.2;margin-bottom:.5rem">⚖</div>
    <div style="color:var(--sub);font-size:.85rem">No debate on record yet.</div>
    <div style="color:var(--muted);font-size:.78rem;margin-top:.3rem">
      9 AI agents — Market Analyst → News Analyst → Bull/Bear×2 → Evaluator → Trader → Risk Analyst → Portfolio Manager
    </div>
    <div id="debateStepBar" class="d-flex flex-wrap justify-content-center gap-2 mt-3"></div>
    <div id="debateOutputs"></div>
  </div>
</div>
{% endif %}

{# Raw log always accessible #}
<details style="margin-top:.75rem">
  <summary style="font-size:.72rem;color:var(--sub);cursor:pointer;user-select:none">▸ Raw analysis log</summary>
  <div style="max-height:200px;overflow-y:auto;margin-top:.5rem">
    <table class="table table-sm mb-0">
      <tbody id="analysisLogBody">
      {% for l in analysis_logs %}
      <tr>
        <td style="width:80px;font-size:.72rem;color:var(--muted);white-space:nowrap">{{ l.created_at.strftime('%H:%M:%S') }}</td>
        <td style="width:55px"><span style="font-size:.68rem;font-weight:700;color:{% if l.level=='ERROR' %}var(--red){% elif l.level=='WARNING' %}var(--amber){% else %}var(--green){% endif %}">{{ l.level }}</span></td>
        <td style="font-size:.75rem;color:var(--text)">{{ l.message }}</td>
      </tr>
      {% else %}
      <tr><td colspan="3" class="text-center py-2" style="color:var(--sub);font-size:.8rem">No logs yet</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</details>

</div>

{# ── TAB: Quantitative Risk/Return ── #}
<div class="tab-pane fade" id="tabQuant">
  {% if opt_stats and opt_stats.price_count and opt_stats.price_count > 10 %}
  <div class="card mb-3">
    <div class="card-header">
      Quantitative Risk / Return
      <small style="color:var(--sub);font-size:.7rem"> — PyPortfolioOpt · {{ opt_stats.price_count }} trading days</small>
    </div>
    <div class="card-body">
      <div class="row g-3">
        {% if opt_stats.expected_annual_return is not none %}
        <div class="col-6 col-md-4">
          <div style="font-size:.73rem;color:var(--sub);margin-bottom:.2rem">Expected Return (ann.)</div>
          <div style="font-size:1.4rem;font-weight:700;color:{% if opt_stats.expected_annual_return|float > 0 %}var(--green){% else %}var(--red){% endif %}">
            {{ '%+.1f'|format(opt_stats.expected_annual_return|float) }}%
          </div>
          <div style="font-size:.7rem;color:var(--muted)">Mean historical annual return</div>
        </div>
        {% endif %}
        {% if opt_stats.annual_volatility is not none %}
        <div class="col-6 col-md-4">
          <div style="font-size:.73rem;color:var(--sub);margin-bottom:.2rem">Annualised Volatility</div>
          <div style="font-size:1.4rem;font-weight:700;color:{% if opt_stats.annual_volatility|float > 30 %}var(--red){% elif opt_stats.annual_volatility|float > 20 %}var(--amber){% else %}var(--green){% endif %}">
            {{ '%.1f'|format(opt_stats.annual_volatility|float) }}%
          </div>
          <div style="font-size:.7rem;color:var(--muted)">&lt;20% low, 20-30% medium, &gt;30% high</div>
        </div>
        {% endif %}
        {% if opt_stats.sharpe_ratio is not none %}
        <div class="col-6 col-md-4">
          <div style="font-size:.73rem;color:var(--sub);margin-bottom:.2rem">Sharpe Ratio</div>
          <div style="font-size:1.4rem;font-weight:700;color:{% if opt_stats.sharpe_ratio|float >= 1 %}var(--green){% elif opt_stats.sharpe_ratio|float >= 0 %}var(--amber){% else %}var(--red){% endif %}">
            {{ '%.2f'|format(opt_stats.sharpe_ratio|float) }}
          </div>
          <div style="font-size:.7rem;color:var(--muted)">&gt;1.0 = good risk-adjusted return (4.5% rf)</div>
        </div>
        {% endif %}
        {% if opt_stats.max_drawdown is not none %}
        <div class="col-6 col-md-4">
          <div style="font-size:.73rem;color:var(--sub);margin-bottom:.2rem">Max Drawdown (3yr)</div>
          <div style="font-size:1.4rem;font-weight:700;color:{% if opt_stats.max_drawdown|float < -30 %}var(--red){% elif opt_stats.max_drawdown|float < -15 %}var(--amber){% else %}var(--green){% endif %}">
            {{ '%.1f'|format(opt_stats.max_drawdown|float) }}%
          </div>
          <div style="font-size:.7rem;color:var(--muted)">Worst peak-to-trough loss</div>
        </div>
        {% endif %}
        {% if opt_stats.momentum_30d is not none %}
        <div class="col-6 col-md-4">
          <div style="font-size:.73rem;color:var(--sub);margin-bottom:.2rem">30-day Momentum</div>
          <div style="font-size:1.4rem;font-weight:700;color:{% if opt_stats.momentum_30d|float > 0 %}var(--green){% else %}var(--red){% endif %}">
            {{ '%+.1f'|format(opt_stats.momentum_30d|float) }}%
          </div>
        </div>
        {% endif %}
        {% if opt_stats.momentum_90d is not none %}
        <div class="col-6 col-md-4">
          <div style="font-size:.73rem;color:var(--sub);margin-bottom:.2rem">90-day Momentum</div>
          <div style="font-size:1.4rem;font-weight:700;color:{% if opt_stats.momentum_90d|float > 0 %}var(--green){% else %}var(--red){% endif %}">
            {{ '%+.1f'|format(opt_stats.momentum_90d|float) }}%
          </div>
        </div>
        {% endif %}
      </div>
    </div>
  </div>
  {% else %}
  <div class="card"><div class="card-body" style="color:var(--sub);text-align:center;padding:2rem">
    Quantitative stats unavailable — need at least 10 days of price history.
  </div></div>
  {% endif %}

  {# Price history table #}
  <div class="card">
    <div class="card-header">Price History (last 30 days)</div>
    <div class="card-body p-0" style="max-height:320px;overflow-y:auto">
      <table class="table table-sm mb-0">
        <thead><tr><th>Date</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th><th>Change</th></tr></thead>
        <tbody>
        {% for p in prices %}
        <tr>
          <td style="font-size:.78rem;color:var(--sub)">{{ p.date }}</td>
          <td>{{ '%.2f'|format(p.open|float) }}</td>
          <td style="color:var(--green)">{{ '%.2f'|format(p.high|float) }}</td>
          <td style="color:var(--red)">{{ '%.2f'|format(p.low|float) }}</td>
          <td><strong>{{ '%.2f'|format(p.close|float) }}</strong></td>
          <td style="color:var(--sub);font-size:.75rem">{{ '{:,.0f}'.format(p.volume|int) if p.volume else '—' }}</td>
          <td style="color:{% if p.chg >= 0 %}var(--green){% else %}var(--red){% endif %}">{{ '%+.2f'|format(p.chg) }}%</td>
        </tr>
        {% else %}
        <tr><td colspan="7" class="text-center py-3" style="color:var(--sub)">No price data</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</div>

{# ── TAB: Fundamentals ── #}
<div class="tab-pane fade" id="tabFundamentals">
  {% if yf_info and (yf_info.market_cap or yf_info.pe_trailing or yf_info.week52_high) %}
  <div class="card mb-3">
    <div class="card-header">Company Fundamentals <small style="color:var(--sub);font-size:.7rem">(yfinance + Alpha Vantage)</small></div>
    <div class="card-body">
      {% if yf_info.description %}
      <p style="font-size:.82rem;color:var(--sub);margin-bottom:1rem;line-height:1.6">{{ yf_info.description }}</p>
      {% endif %}
      {% if yf_info.sector or yf_info.industry %}
      <p style="font-size:.78rem;color:var(--sub);margin-bottom:.75rem">
        {% if yf_info.sector %}<strong>Sector:</strong> {{ yf_info.sector }}{% endif %}
        {% if yf_info.industry %} &nbsp;·&nbsp; <strong>Industry:</strong> {{ yf_info.industry }}{% endif %}
      </p>
      {% endif %}
      <div class="row g-3">
        {% set fundamentals = [
          ('Market Cap',     yf_info.market_cap,      'mc'),
          ('P/E (TTM)',      yf_info.pe_trailing,     'num'),
          ('Forward P/E',   yf_info.pe_forward,      'num'),
          ('Div Yield',     yf_info.dividend_yield,  'pct'),
          ('Beta',          yf_info.beta,            'num'),
          ('52W High',      yf_info.week52_high,     'price'),
          ('52W Low',       yf_info.week52_low,      'price'),
          ('Rev Growth QoQ',yf_info.revenue_growth,  'pct_signed'),
          ('Gross Profit',  yf_info.gross_margin,    'mc'),
          ('ROE',           yf_info.roe,             'pct'),
          ('Analyst Target',yf_info.analyst_target,  'price'),
          ('Employees',     yf_info.employees,       'int'),
        ] %}
        {% for label, val, fmt in fundamentals if val %}
        <div class="col-6 col-md-3">
          <div style="font-size:.72rem;color:var(--sub);margin-bottom:.15rem">{{ label }}</div>
          <div style="font-size:.9rem;font-weight:600">
            {% if fmt == 'mc' %}
              {% set mc = val|float %}
              {% if mc >= 1000000000000 %}{{ '%.1f'|format(mc/1000000000000) }}T
              {% elif mc >= 1000000000 %}{{ '%.1f'|format(mc/1000000000) }}B
              {% elif mc >= 1000000 %}{{ '%.0f'|format(mc/1000000) }}M
              {% else %}{{ '%.0f'|format(mc) }}{% endif %}
            {% elif fmt == 'pct' %}<span style="color:var(--green)">{{ '%.1f'|format(val|float*100) }}%</span>
            {% elif fmt == 'pct_signed' %}<span style="color:{% if val|float > 0 %}var(--green){% else %}var(--red){% endif %}">{{ '%+.1f'|format(val|float*100) }}%</span>
            {% elif fmt == 'price' %}{{ '%.2f'|format(val|float) }}
            {% elif fmt == 'int' %}{{ '%d'|format(val|int) }}
            {% elif fmt == 'text' %}<span style="text-transform:capitalize">{{ val|string|replace('_',' ') }}</span>
            {% else %}{{ '%.2f'|format(val|float) }}{% endif %}
          </div>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>
  {% else %}
  <div class="card mb-3"><div class="card-body" style="color:var(--sub);text-align:center;padding:2rem">
    Fundamental data unavailable for {{ ticker }}.
  </div></div>
  {% endif %}

  {% if in_holdings and holding %}
  <div class="card">
    <div class="card-header">Your Position</div>
    <div class="card-body">
      <div class="row g-3">
        <div class="col-6 col-md-3">
          <div style="font-size:.72rem;color:var(--sub)">Shares held</div>
          <div style="font-size:1.1rem;font-weight:700">{{ holding.shares }}</div>
        </div>
        <div class="col-6 col-md-3">
          <div style="font-size:.72rem;color:var(--sub)">Avg cost</div>
          <div style="font-size:1.1rem;font-weight:700">{{ holding.currency }} {{ '%.2f'|format(holding.avg_buy_price|float) }}</div>
        </div>
        <div class="col-6 col-md-3">
          <div style="font-size:.72rem;color:var(--sub)">Current price</div>
          <div style="font-size:1.1rem;font-weight:700">{{ price_cur }}</div>
        </div>
        <div class="col-6 col-md-3">
          <div style="font-size:.72rem;color:var(--sub)">Unrealised P&amp;L</div>
          <div style="font-size:1.1rem;font-weight:700;color:{% if pnl_eur >= 0 %}var(--green){% else %}var(--red){% endif %}">
            {{ '+' if pnl_eur >= 0 else '' }}&#8364;{{ '%.2f'|format(pnl_eur) }}
            ({{ '+' if pnl_pct >= 0 else '' }}{{ '%.1f'|format(pnl_pct) }}%)
          </div>
        </div>
      </div>
    </div>
  </div>
  {% endif %}
</div>

{# ── TAB: GDELT & News ── #}
<div class="tab-pane fade" id="tabGdelt">
  <div class="card mb-3">
    <div class="card-header">GDELT Sentiment (last 14 days)</div>
    <div class="card-body p-0">
      <table class="table table-sm mb-0">
        <thead><tr><th>Date</th><th>Articles</th><th>Tone</th><th>Velocity</th><th>Sentiment</th><th>Accel</th></tr></thead>
        <tbody>
        {% for s in sentiment %}
        <tr>
          <td style="font-size:.78rem;color:var(--sub)">{{ s.date }}</td>
          <td>{{ s.article_count or 0 }}</td>
          <td style="color:{% if (s.avg_tone|float)>0 %}var(--green){% elif (s.avg_tone|float)<0 %}var(--red){% else %}var(--sub){% endif %}">
            {{ '%+.2f'|format(s.avg_tone|float) }}
          </td>
          <td style="color:var(--text)">{{ '%.1f'|format(s.mention_velocity|float) }}x</td>
          <td style="font-size:.75rem">
            {% if s.finbert_sentiment_label %}
            <span style="color:{% if s.finbert_sentiment_label=='positive' %}var(--green){% elif s.finbert_sentiment_label=='negative' %}var(--red){% else %}var(--sub){% endif %}">
              {{ s.finbert_sentiment_label }} ({{ '%.0f'|format((s.finbert_confidence|float)*100) }}%)
            </span>
            {% else %}<span style="color:var(--muted)">—</span>{% endif %}
          </td>
          <td>{% if s.is_accelerating %}<span style="color:var(--green)">&#8679;</span>{% else %}<span style="color:var(--muted)">—</span>{% endif %}</td>
        </tr>
        {% else %}
        <tr><td colspan="6" class="text-center py-4" style="color:var(--sub)">
          Not yet tracked — add {{ ticker }} to watchlist to collect GDELT data.
          <form method="post" action="/watchlist/add" style="display:inline;margin-left:.5rem">
            <input type="hidden" name="ticker" value="{{ ticker }}">
            <input type="hidden" name="company_name" value="{{ company }}">
            <button class="btn btn-sm btn-outline-secondary" style="font-size:.75rem">Watch {{ ticker }}</button>
          </form>
        </td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  {% if top_themes %}
  <div class="card mb-3">
    <div class="card-header">GDELT Themes</div>
    <div class="card-body">
      <div class="d-flex flex-wrap gap-1">
        {% for theme in top_themes %}
        <span class="badge bg-secondary" style="font-size:.7rem;font-weight:400">{{ theme }}</span>
        {% endfor %}
      </div>
    </div>
  </div>
  {% endif %}

  {% if yf_news %}
  <div class="card mb-3">
    <div class="card-header">Recent News</div>
    <div class="card-body p-0">
      <table class="table table-sm mb-0">
        <tbody>
        {% for n in yf_news %}
        <tr>
          <td style="width:75px;font-size:.72rem;color:var(--muted);white-space:nowrap">{{ n.date }}</td>
          <td style="font-size:.82rem">
            {% if n.link %}<a href="{{ n.link }}" target="_blank" rel="noopener" style="color:var(--text);text-decoration:none">{{ n.title }}</a>
            {% else %}{{ n.title }}{% endif %}
          </td>
          <td style="width:90px;font-size:.7rem;color:var(--sub);text-align:right">{{ n.publisher }}</td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}

  {% if poly_signals %}
  <div class="card">
    <div class="card-header">Polymarket Signals (7 days)</div>
    <div class="card-body p-0">
      <table class="table table-sm mb-0">
        <thead><tr><th>Question</th><th style="width:80px;text-align:right">Probability</th><th style="width:70px">Date</th></tr></thead>
        <tbody>
        {% for p in poly_signals %}
        <tr>
          <td style="font-size:.8rem">{{ p.question }}</td>
          <td style="text-align:right;font-weight:700;font-size:.85rem;color:{% if p.probability|float > 0.6 %}var(--green){% elif p.probability|float < 0.4 %}var(--red){% else %}var(--amber){% endif %}">
            {{ '%.0f'|format(p.probability|float*100) }}%
          </td>
          <td style="font-size:.72rem;color:var(--sub)">{{ p.date }}</td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}
</div>

{# ── TAB: Raw Data ── #}
<div class="tab-pane fade" id="tabRaw">
  <div class="card mb-3">
    <div class="card-header">Discovery Candidates (today)</div>
    <div class="card-body p-0">
      <div class="table-responsive">
      <table class="table table-sm mb-0">
        <thead><tr><th>Direction</th><th>Event</th><th>Score</th><th>Quant</th><th>Blended</th><th>Eligible</th><th>Reason</th></tr></thead>
        <tbody>
        {% for c in candidates %}
        <tr>
          <td>
            {% if c.direction == 'SELL' and not in_holdings %}
            <span class="badge bg-secondary">HEADWIND</span>
            {% else %}
            <span class="badge badge-{{ c.direction|lower }}">{{ c.direction }}</span>
            {% endif %}
          </td>
          <td style="font-size:.78rem;color:var(--sub)">{{ c.event_type }}</td>
          <td><strong>{{ '%.0f'|format(c.total_score|float) }}</strong></td>
          <td style="color:var(--sub)">{{ '%.0f'|format(c.quant_score|float) if c.quant_score else '—' }}</td>
          <td style="color:var(--accent)">{{ '%.0f'|format(c.blended_score|float) if c.blended_score else '—' }}</td>
          <td>{% if c.eligible %}<span style="color:var(--green)">&#10003;</span>{% else %}<span style="color:var(--red)">&#10007;</span>{% endif %}</td>
          <td style="font-size:.75rem;color:var(--sub)" title="{{ c.reason or '' }}">{{ (c.reason or '')[:100] }}</td>
        </tr>
        {% else %}
        <tr><td colspan="7" class="text-center py-3" style="color:var(--sub)">No candidates today</td></tr>
        {% endfor %}
        </tbody>
      </table>
      </div>
    </div>
  </div>

  <div class="d-flex gap-2">
    <a href="/signals" class="btn btn-sm btn-outline-secondary" style="font-size:.78rem">All signals today</a>
    <a href="/data" class="btn btn-sm btn-outline-secondary" style="font-size:.78rem">Data health</a>
    <a href="/history" class="btn btn-sm btn-outline-secondary" style="font-size:.78rem">Recommendation history</a>
  </div>
</div>

</div>{# end tab-content #}

<script>
(function() {
  var ticker = '{{ ticker }}';
  var analyzeBtn  = document.getElementById('analyzeNowBtn');
  var analyzeForm = document.getElementById('analyzeNowForm');
  var queueBanner = document.getElementById('queueBanner');
  var stepBar     = document.getElementById('debateStepBar');
  var outputsBox  = document.getElementById('debateOutputs');
  var debateSpinner = document.getElementById('debateSpinner');

  var STEP_COLORS = {1:'#60a5fa',2:'#a78bfa',3:'#34d399',4:'#f87171',5:'#fbbf24',6:'#f472b6'};
  var STEP_ICONS  = {1:'📈',2:'🔬',3:'🟢',4:'🔴',5:'⚠️',6:'🎯'};

  function renderStepBar(steps, stepActive, stepsDone) {
    if (!stepBar) return;
    var done = new Set((stepsDone||[]).map(s => s.num));
    var html = '';
    (steps||[]).forEach(function(s) {
      var isDone = done.has(s.num), isActive = (s.num === stepActive) && !isDone;
      var col = STEP_COLORS[s.num] || '#888', bg, border, fw;
      if (isActive)     { bg=col+'26'; border=col; fw='700'; }
      else if (isDone)  { bg='rgba(34,197,94,.12)'; border='var(--green)'; col='var(--green)'; fw='500'; }
      else              { bg='var(--surface2)'; border='var(--border)'; col='var(--muted)'; fw='400'; }
      html += '<div style="display:flex;align-items:center;gap:.3rem;padding:.28rem .6rem;border-radius:999px;border:1px solid '+border+';background:'+bg+';font-size:.72rem;color:'+col+';font-weight:'+fw+'">' +
              (isActive ? '<span class="spinner-border" style="width:.65rem;height:.65rem;border-width:2px"></span>' : '<span>'+(isDone?'✓':s.num)+'</span>') +
              '<span>'+s.name+'</span></div>';
    });
    stepBar.innerHTML = html || '<span style="color:var(--sub);font-size:.8rem">No analysis running</span>';
  }

  function renderStepOutputs(stepsDone) {
    if (!outputsBox) return;
    if (!stepsDone || !stepsDone.length) { outputsBox.innerHTML=''; return; }
    var html = '';
    stepsDone.forEach(function(s) {
      var col = STEP_COLORS[s.num]||'#888', icon = STEP_ICONS[s.num]||'●';
      html += '<div style="border:1px solid '+col+'30;border-left:3px solid '+col+';border-radius:6px;padding:.6rem .8rem;margin-bottom:.5rem;background:'+col+'0a">' +
              '<div style="font-size:.7rem;font-weight:700;color:'+col+';margin-bottom:.3rem">'+icon+' '+s.name+'</div>' +
              '<div style="font-size:.8rem;color:var(--text);line-height:1.6;white-space:pre-wrap">'+s.text.replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</div></div>';
    });
    outputsBox.innerHTML = html;
  }

  function setAnalyzeState(data) {
    if (!analyzeBtn) return;
    var isActive = data.running || data.active === ticker;
    var inQueue  = data.in_queue, queuePos = data.queue_pos;

    analyzeBtn.disabled = isActive || (inQueue && queuePos > 0);
    analyzeBtn.textContent = isActive ? '⏳ Analyzing…' : (inQueue&&queuePos>0 ? '⏳ Queued #'+queuePos : '⚙ Analyze Now');

    if (queueBanner) {
      if (isActive) {
        queueBanner.style.display='block'; queueBanner.className='alert alert-info py-2 px-3 mb-3';
        queueBanner.innerHTML='⏳ <strong>'+ticker+'</strong> — debate running. Steps update in real time below.';
      } else if (inQueue && queuePos>0) {
        queueBanner.style.display='block'; queueBanner.className='alert alert-warning py-2 px-3 mb-3';
        var am = data.active ? ' Currently: <strong>'+data.active+'</strong>.' : '';
        queueBanner.innerHTML='🕐 Queued at position '+queuePos+'.'+am;
      } else {
        queueBanner.style.display='none';
      }
    }
    if (debateSpinner) debateSpinner.classList.toggle('d-none', !isActive);

    var debate = data.debate||{};
    var stepActive = isActive ? (debate.step_active||0) : 0;
    var stepsDone  = debate.steps_done||[];
    renderStepBar(data.debate_steps||[], stepActive, stepsDone);
    renderStepOutputs(stepsDone);

    var note = document.getElementById('logRefreshNote');
    if (note) {
      if (isActive) note.textContent='Running — updates every 4s';
      else if (inQueue) note.textContent='Waiting in queue…';
      else if (stepsDone.length) note.textContent='Done — reload page for updated recommendation';
      else note.textContent='Click Analyze Now to run the 9-agent pipeline (~5 min)';
    }

    var body = document.getElementById('analysisLogBody');
    if (body && data.logs && data.logs.length) {
      var lhtml='';
      data.logs.forEach(function(l) {
        var col=l.level==='ERROR'?'var(--red)':l.level==='WARNING'?'var(--amber)':'var(--green)';
        lhtml+='<tr><td style="width:80px;font-size:.72rem;color:var(--muted);white-space:nowrap">'+l.time+'</td>'+
               '<td style="width:55px"><span style="font-size:.68rem;font-weight:700;color:'+col+'">'+l.level+'</span></td>'+
               '<td style="font-size:.75rem">'+l.message+'</td></tr>';
      });
      body.innerHTML=lhtml;
    }
  }

  if (analyzeForm) {
    analyzeForm.addEventListener('submit', function() {
      if (analyzeBtn) { analyzeBtn.disabled=true; analyzeBtn.textContent='⏳ Queuing…'; }
    });
  }

  // Rate limit quota badge next to Analyze Now button
  function refreshQuota() {
    fetch('/api/rate-limit-status').then(r=>r.json()).then(function(q) {
      var el = document.getElementById('ondemandQuota');
      if (!el) return;
      if (!q.ok) { el.textContent=''; return; }
      var rem = q.ondemand_ticker_remaining !== undefined ? q.ondemand_ticker_remaining : '?';
      if (rem === 0) {
        el.innerHTML='<span style="color:var(--red)">On-demand quota full (0 left today)</span>';
        if (analyzeBtn && !analyzeBtn.disabled) {
          analyzeBtn.disabled = true;
          analyzeBtn.title = 'On-demand OR quota exhausted for today';
        }
      } else {
        el.innerHTML='<span style="color:var(--sub)">' + rem + ' on-demand ticker' + (rem===1?'':'s') + ' remaining today</span>';
      }
    }).catch(function(){});
  }

  function refreshLogs() {
    fetch('/api/analysis-logs/'+ticker)
      .then(r=>r.json())
      .then(function(data) {
        setAnalyzeState(data);
        setTimeout(refreshLogs, (data.running||data.in_queue) ? 4000 : 20000);
      })
      .catch(function() { setTimeout(refreshLogs, 8000); });
  }

  fetch('/api/analysis-logs/'+ticker).then(r=>r.json()).then(function(data) {
    setAnalyzeState(data);
    setTimeout(refreshLogs, (data.running||data.in_queue) ? 4000 : 20000);
  }).catch(function() { setTimeout(refreshLogs, 20000); });

  refreshQuota();
})();
</script>
"""


@app.route('/ticker/<ticker>')
def ticker_detail(ticker):
    ticker = ticker.upper().strip()

    meta_row = query("""
        SELECT company_name, sector, country, exchange
        FROM universe WHERE ticker=%s LIMIT 1
    """, (ticker,))
    meta = meta_row[0] if meta_row else {}

    company = (meta.get('company_name') or ticker) if meta else ticker

    # Latest recommendation (with all debate fields)
    latest_rec = query("""
        SELECT date, action, confidence, reasoning, signal_sources,
               debate_trace, bull_case, bear_case, key_risks, time_horizon
        FROM recommendations
        WHERE ticker=%s AND action != 'HOLD'
        ORDER BY date DESC, created_at DESC LIMIT 1
    """, (ticker,))
    latest_rec = latest_rec[0] if latest_rec else None

    # GDELT sentiment (14 days)
    sentiment = query("""
        SELECT date, article_count, avg_tone, mention_velocity, tone_trajectory,
               is_accelerating, finbert_sentiment_label, finbert_confidence, top_themes
        FROM news_sentiment
        WHERE ticker=%s ORDER BY date DESC LIMIT 14
    """, (ticker,))

    # Collect all themes
    all_themes = set()
    for s in sentiment:
        if s['top_themes']:
            for t in s['top_themes'].split(','):
                t = t.strip()
                if t:
                    all_themes.add(t)
    top_themes = sorted(all_themes)[:20]

    # GDELT velocity (latest)
    velocity = '—'
    if sentiment:
        v = sentiment[0]['mention_velocity']
        velocity = f'{float(v):.1f}' if v else '1.0'

    # Holding status
    holding_row = query("""
        SELECT ticker, shares, avg_buy_price, currency
        FROM holdings WHERE ticker=%s AND active=TRUE LIMIT 1
    """, (ticker,))
    in_holdings = bool(holding_row)
    holding = holding_row[0] if holding_row else None

    # Price data
    price_rows = query("""
        SELECT date, open, high, low, close, volume
        FROM prices WHERE ticker=%s ORDER BY date DESC LIMIT 30
    """, (ticker,))

    # Live price fallback — fetch from yfinance if not in DB
    live_price = None
    if not price_rows:
        try:
            import yfinance as yf
            from db.database import track_api_call as _tac
            _tac('yfinance')
            hist = yf.Ticker(ticker).history(period='2d')
            if not hist.empty:
                live_price = float(hist['Close'].iloc[-1])
        except Exception:
            pass

    prices_with_chg = []
    for i, p in enumerate(price_rows):
        prev_close = float(price_rows[i+1]['close']) if i+1 < len(price_rows) else float(p['close'])
        cur_close = float(p['close'])
        chg = ((cur_close - prev_close) / prev_close * 100) if prev_close else 0.0
        prices_with_chg.append(dict(p, chg=round(chg, 2)))

    fx = get_live_fx()

    # Current price and P&L
    price_cur = '—'
    pnl_pct = 0.0
    pnl_eur = 0.0
    latest_close = None
    if price_rows:
        latest_close = float(price_rows[0]['close'])
        price_cur = f'{latest_close:.2f}'
    elif live_price:
        latest_close = live_price
        price_cur = f'{live_price:.2f} (live)'
    if latest_close:

        if in_holdings and holding:
            avg = float(holding['avg_buy_price'] or 0)
            shares = float(holding['shares'])
            currency = holding['currency'] or 'USD'
            rate = fx.get(currency, fx['USD'])
            pnl_pct = ((latest_close - avg) / avg * 100) if avg else 0.0
            pnl_eur = (latest_close - avg) * shares * rate
        elif latest_rec and latest_rec['date']:
            # vs price at recommendation date
            rec_price = query("""
                SELECT close FROM prices WHERE ticker=%s AND date >= %s
                ORDER BY date ASC LIMIT 1
            """, (ticker, latest_rec['date']))
            if rec_price:
                rp = float(rec_price[0]['close'])
                pnl_pct = ((latest_close - rp) / rp * 100) if rp else 0.0

    # Discovery candidates
    candidates = query("""
        SELECT direction, event_type, total_score, quant_score,
               blended_score, reason, eligible
        FROM discovery_candidates
        WHERE ticker=%s AND date=%s ORDER BY total_score DESC
    """, (ticker, date.today()))

    # Polymarket signals mentioning this ticker
    poly_signals = query("""
        SELECT question, probability, date
        FROM polymarket_signals
        WHERE relevant_tickers LIKE %s
          AND date >= CURRENT_DATE - INTERVAL '7 days'
        ORDER BY date DESC, probability DESC LIMIT 10
    """, (f'%{ticker}%',))

    # Fundamentals — from shared cache (pre-populated by daily pipeline)
    yf_info = {}
    yf_news = []
    try:
        yf_info = _build_yf_info(ticker)
    except Exception:
        pass

    # News via yfinance (separate try — works independently of .info)
    try:
        import yfinance as yf
        raw_news = yf.Ticker(ticker).news or []
        for n in raw_news[:8]:
            ct = n.get('content') or {}
            title = ct.get('title') or n.get('title', '')
            pub   = (ct.get('provider') or {}).get('displayName') or n.get('publisher', '')
            ts    = ct.get('pubDate') or ''
            link  = (ct.get('canonicalUrl') or {}).get('url') or n.get('link', '')
            if title:
                yf_news.append({'title': title, 'publisher': pub, 'date': ts[:10], 'link': link})
    except Exception:
        pass

    # PyPortfolioOpt single-ticker stats
    opt_stats = {}
    try:
        from portfolio.optimizer import get_ticker_stats
        opt_stats = get_ticker_stats(ticker)
    except Exception:
        pass

    # Watchlist status
    wl_row = query("SELECT id FROM watchlist WHERE ticker=%s AND active=TRUE", (ticker,))
    in_watchlist = bool(wl_row)

    # Pre-fill trade modal from recommendation
    rec_shares = 0
    rec_price = 0.0
    if latest_rec and price_rows:
        rec_price = round(float(price_rows[0]['close']), 2)
        # Suggest shares if we have a BUY rec: use 10% of cash / price as a rough default
        cash = get_cash()
        fx = get_live_fx()
        currency = holding['currency'] if holding else 'USD'
        rate = fx.get(currency, fx['USD'])
        if rec_price > 0 and rate > 0:
            budget = cash * 0.1
            rec_shares = round(budget / (rec_price * rate), 2) if rate else 0

    # Analysis logs for this ticker (last 2h)
    analysis_logs = query("""
        SELECT level, message, created_at FROM system_logs
        WHERE component='trading_agents'
          AND message LIKE %s
          AND created_at >= NOW() - INTERVAL '2 hours'
        ORDER BY created_at ASC LIMIT 30
    """, (f'%{ticker}%',))
    analysis_running = any(
        '[ON-DEMAND]' in (r['message'] or '') and 'DONE' not in r['message']
        for r in analysis_logs[-5:]
    )

    natural_summary = _natural_summary(latest_rec) if latest_rec else ''

    # Parse debate trace JSON into dict for structured display
    import json as _json
    debate_trace = None
    if latest_rec and latest_rec.get('debate_trace'):
        try:
            debate_trace = _json.loads(latest_rec['debate_trace'])
        except Exception:
            pass

    return render_template_string(
        make_page(_TICKER_DETAIL, f'{ticker} — {company}'),
        current_page='',
        ticker=ticker, company=company, meta=meta,
        latest_rec=latest_rec, natural_summary=natural_summary,
        debate_trace=debate_trace,
        sentiment=sentiment, top_themes=top_themes, velocity=velocity,
        in_holdings=in_holdings, holding=holding,
        in_watchlist=in_watchlist,
        prices=prices_with_chg, price_cur=price_cur,
        pnl_pct=round(pnl_pct, 2), pnl_eur=round(pnl_eur, 2),
        candidates=candidates,
        poly_signals=poly_signals,
        yf_info=yf_info, yf_news=yf_news,
        rec_shares=rec_shares, rec_price=rec_price,
        analysis_logs=analysis_logs, analysis_running=analysis_running,
        opt_stats=opt_stats,
        today=date.today().isoformat(),
        health=get_system_health(),
    )


@app.route('/ticker/<ticker>/action', methods=['POST'])
def ticker_action(ticker):
    """Quick BUY/SELL/WATCH action from ticker detail page — logs to watchlist/signal."""
    ticker = ticker.upper().strip()
    action = request.form.get('action', '').upper()
    if action == 'WATCH':
        execute("""
            INSERT INTO watchlist (ticker, active) VALUES (%s, TRUE)
            ON CONFLICT (ticker) DO UPDATE SET active=TRUE
        """, (ticker,))
        flash(f'{ticker} added to watchlist.')
        log('INFO', 'dashboard', f'User added {ticker} to watchlist via ticker detail')
    elif action in ('BUY', 'SELL'):
        flash(f'{action} logged for {ticker}. Log the completed trade via the Portfolio page.')
        log('INFO', 'dashboard', f'User triggered {action} for {ticker} from ticker detail')
    return redirect(url_for('ticker_detail', ticker=ticker))


# ── Selective TradingAgents re-analysis ───────────────────────────────────────

@app.route('/reanalyze/<ticker>', methods=['POST'])
def reanalyze(ticker):
    """Re-analysis — delegates to the same on-demand queue as Analyze Now."""
    ticker = ticker.upper().strip()
    log('ta_wrapper', 'info', f'[RE-ANALYSIS] {ticker} routed to on-demand queue')
    # Reuse the same on-demand pipeline
    return analyze_ticker(ticker)


# ── Accuracy / performance tracking ──────────────────────────────────────────

_ACCURACY = """
<div class="page-header">System Accuracy &amp; Performance</div>

<div class="row g-3 mb-3">
  {% set latest = metrics[0] if metrics else None %}
  <div class="col-6 col-md-3">
    <div class="card metric-card" style="border-top:3px solid var(--green)">
      <div class="metric-value" style="color:var(--green)">
        {{ '%.0f'|format((latest.hit_rate|float)*100) if latest else '—' }}%
      </div>
      <div class="metric-label">Hit Rate</div>
    </div>
  </div>
  <div class="col-6 col-md-3">
    <div class="card metric-card" style="border-top:3px solid var(--accent)">
      <div class="metric-value">
        {% if latest %}&#8364;{{ '%.0f'|format(latest.cumulative_pnl|float) }}{% else %}—{% endif %}
      </div>
      <div class="metric-label">Cumulative PnL (10k base)</div>
    </div>
  </div>
  <div class="col-6 col-md-3">
    <div class="card metric-card" style="border-top:3px solid var(--amber)">
      <div class="metric-value" style="color:var(--amber)">
        {{ '%.2f'|format(latest.sharpe|float) if latest else '—' }}
      </div>
      <div class="metric-label">Sharpe Ratio</div>
    </div>
  </div>
  <div class="col-6 col-md-3">
    <div class="card metric-card" style="border-top:3px solid var(--red)">
      <div class="metric-value" style="color:var(--red)">
        {{ '%.1f'|format((latest.max_drawdown|float)*100) if latest else '—' }}%
      </div>
      <div class="metric-label">Max Drawdown</div>
    </div>
  </div>
</div>

<div class="row g-3 mb-3">
  <div class="col-md-6">
    <div class="card">
      <div class="card-header">Strategy Metrics History</div>
      <div class="card-body p-0" style="max-height:320px;overflow-y:auto">
        <table class="table table-sm mb-0">
          <thead><tr>
            <th>Date</th><th>Hit Rate</th><th>Cum PnL</th>
            <th>Sharpe</th><th>Recs</th><th>Wins</th>
          </tr></thead>
          <tbody>
          {% for m in metrics %}
          <tr>
            <td style="font-size:.78rem">{{ m.date }}</td>
            <td style="color:{% if (m.hit_rate|float) >= 0.5 %}var(--green){% else %}var(--red){% endif %}">
              {{ '%.0f'|format((m.hit_rate|float)*100) }}%
            </td>
            <td style="color:{% if (m.cumulative_pnl|float) >= 0 %}var(--green){% else %}var(--red){% endif %}">
              &#8364;{{ '%.0f'|format(m.cumulative_pnl|float) }}
            </td>
            <td>{{ '%.2f'|format(m.sharpe|float) }}</td>
            <td>{{ m.total_recommendations }}</td>
            <td>{{ m.profitable_buys }}</td>
          </tr>
          {% else %}
          <tr><td colspan="6" class="text-center text-muted py-3">
            No metrics yet — runs after 30 days of recommendations
          </td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="col-md-6">
    <div class="card">
      <div class="card-header">Portfolio Value History</div>
      <div class="card-body p-0" style="max-height:320px;overflow-y:auto">
        <table class="table table-sm mb-0">
          <thead><tr><th>Date</th><th>Portfolio Value</th><th>Cash</th><th>Change</th></tr></thead>
          <tbody>
          {% for i, p in perf %}
          <tr>
            <td style="font-size:.78rem">{{ p.date }}</td>
            <td><strong>&#8364;{{ '%.0f'|format(p.portfolio_value|float) }}</strong></td>
            <td style="color:var(--muted)">&#8364;{{ '%.0f'|format(p.cash|float) }}</td>
            <td>
              {% if i > 0 %}
                {% set prev = perf[i-1][1] %}
                {% set total_cur = p.portfolio_value|float + p.cash|float %}
                {% set total_prev = prev.portfolio_value|float + prev.cash|float %}
                {% if total_prev > 0 %}
                  {% set chg = (total_cur - total_prev) / total_prev * 100 %}
                  <span style="color:{% if chg >= 0 %}var(--green){% else %}var(--red){% endif %}">
                    {{ '%+.1f'|format(chg) }}%
                  </span>
                {% endif %}
              {% else %}—{% endif %}
            </td>
          </tr>
          {% else %}
          <tr><td colspan="4" class="text-center text-muted py-3">No history yet</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="card mb-3">
  <div class="card-header">Per-Ticker Backtest Results</div>
  <div class="card-body p-0" style="max-height:320px;overflow-y:auto">
    <table class="table table-sm mb-0">
      <thead><tr>
        <th>Ticker</th><th>Period</th><th>Win Rate</th>
        <th>Sharpe</th><th>Max DD</th><th>Total Return</th>
      </tr></thead>
      <tbody>
      {% for b in backtests %}
      <tr>
        <td><a href="/ticker/{{ b.ticker }}" style="color:var(--accent);text-decoration:none;font-weight:600">{{ b.ticker }}</a></td>
        <td style="font-size:.75rem;color:var(--muted)">{{ b.period_start }} → {{ b.period_end }}</td>
        <td style="color:{% if (b.win_rate|float) >= 0.5 %}var(--green){% else %}var(--red){% endif %}">
          {{ '%.0f'|format((b.win_rate|float)*100) }}%
        </td>
        <td>{{ '%.2f'|format(b.sharpe_ratio|float) }}</td>
        <td style="color:var(--red)">{{ '%.1f'|format((b.max_drawdown|float)*100) }}%</td>
        <td style="color:{% if (b.total_return|float) >= 0 %}var(--green){% else %}var(--red){% endif %}">
          {{ '%+.1f'|format((b.total_return|float)*100) }}%
        </td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="text-center text-muted py-3">
        No backtests yet — runs weekly on Sundays
      </td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
"""


@app.route('/accuracy')
def accuracy():
    metrics = query("""
        SELECT date, hit_rate, cumulative_pnl, sharpe, max_drawdown,
               total_recommendations, profitable_buys, loss_avoided_sells
        FROM strategy_metrics ORDER BY date DESC LIMIT 30
    """)

    perf_rows = query("""
        SELECT date, portfolio_value, cash
        FROM performance_history ORDER BY date DESC LIMIT 30
    """)
    perf = list(enumerate(perf_rows))

    backtests = query("""
        SELECT ticker, strategy, period_start, period_end,
               win_rate, sharpe_ratio, max_drawdown, total_return
        FROM backtest_results ORDER BY created_at DESC LIMIT 50
    """)

    return render_template_string(
        make_page(_RESEARCH_SUBNAV + _ACCURACY, 'Research — Accuracy'),
        current_page='Research', research_page='accuracy',
        metrics=metrics, perf=perf, backtests=backtests,
        health=get_system_health(),
    )


@app.route('/update-cash', methods=['POST'])
def update_cash():
    try:
        new_cash = float(request.form.get('cash', 0) or 0)
        _upsert_setting('nordnet_cash_eur', str(round(new_cash, 2)))
        log('INFO', 'dashboard', f'Cash updated to €{new_cash:.2f}')
        flash(f'Cash updated to €{new_cash:,.2f}')
    except Exception as e:
        flash(f'Failed to update cash: {e}')
    return redirect(url_for('index'))


def _pipeline_is_running():
    """Return True if run_daily.sh is currently holding the pipeline lock."""
    import fcntl, os
    lock_path = '/tmp/advisor_daily.lock'
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return False  # lock acquired → pipeline not running
    except (IOError, OSError):
        return True   # lock held by run_daily.sh


@app.route('/reload-universe', methods=['POST'])
def reload_universe():
    """Trigger universe reload (monthly task) in the background."""
    import subprocess, os
    if _pipeline_is_running():
        flash('Pipeline is already running — wait for it to finish first.')
        return redirect(url_for('pipeline'))
    script = '/home/ubuntu/advisor/reload_universe.sh'
    log_path = '/home/ubuntu/advisor/logs/daily.log'
    try:
        env = {**os.environ, 'TERM': 'dumb', 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}'}
        subprocess.Popen(
            ['systemd-run', '--scope', '--user', '-u', 'advisor-universe.scope',
             'bash', script],
            stdout=open(log_path, 'a'),
            stderr=subprocess.STDOUT,
            env=env,
        )
        log('INFO', 'dashboard', 'Universe reload started')
        flash('Universe reload started — this takes several minutes.')
    except Exception as e:
        log('WARNING', 'dashboard', f'systemd-run failed, falling back: {e}')
        try:
            subprocess.Popen(
                ['bash', script],
                stdout=open(log_path, 'a'),
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, 'TERM': 'dumb'},
            )
            flash('Universe reload started — this takes several minutes.')
        except Exception as e2:
            flash(f'Failed to start universe reload: {e2}')
    return redirect(url_for('pipeline'))


@app.route('/run-pipeline', methods=['POST'])
def run_pipeline():
    """Trigger the full daily pipeline in the background."""
    import subprocess, os
    if _pipeline_is_running():
        flash('Pipeline is already running — check the status below.')
        return redirect(url_for('pipeline'))
    script = '/home/ubuntu/advisor/run_daily.sh'
    log_path = '/home/ubuntu/advisor/logs/daily.log'
    try:
        env = {**os.environ, 'TERM': 'dumb', 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}'}
        # Launch in a separate user-scope so the pipeline's memory usage is
        # accounted outside the dashboard's cgroup and cannot OOM-kill the dashboard.
        subprocess.Popen(
            ['systemd-run', '--scope', '--user', '-u', 'advisor-pipeline.scope',
             'bash', script],
            stdout=open(log_path, 'a'),
            stderr=subprocess.STDOUT,
            env=env,
        )
        log('INFO', 'dashboard', 'Pipeline started')
        flash('Pipeline started.')
    except Exception as e:
        log('WARNING', 'dashboard', f'systemd-run failed, falling back to direct launch: {e}')
        try:
            subprocess.Popen(
                ['bash', script],
                stdout=open(log_path, 'a'),
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, 'TERM': 'dumb'},
            )
            log('INFO', 'dashboard', 'Pipeline started (direct)')
            flash('Pipeline started.')
        except Exception as e2:
            flash(f'Failed to start pipeline: {e2}')
    return redirect(url_for('pipeline'))


def _run_analysis_background(ticker):
    """Background worker: run 9-agent Groq+OpenRouter debate for on-demand analysis."""
    from db.database import log as blog

    with _analysis_lock:
        _analysis_state['debate'][ticker] = {
            'step_active': 1,
            'steps_done': [],
        }

    def _step(num, name, text=''):
        with _analysis_lock:
            d = _analysis_state['debate'].setdefault(ticker, {'step_active': 0, 'steps_done': []})
            d['steps_done'].append({'num': num, 'name': name, 'text': text})
            d['step_active'] = num + 1

    try:
        from analysis.trading_agents_wrapper import analyze_ondemand, save_recommendations
        from analysis.rate_limit_manager import get_manager
        from pipeline.signal_engine import compute_conviction

        blog('INFO', 'trading_agents', f'[ON-DEMAND] Starting analysis for {ticker}')

        # Check quota before starting
        rate_mgr = get_manager()
        if not rate_mgr.can_run_ticker('ondemand'):
            blog('WARNING', 'trading_agents', f'[ON-DEMAND] {ticker}: on-demand quota exhausted')
            _step(1, 'Quota check', 'On-demand analysis quota exhausted for today.')
            return

        _step(1, 'Signal engine', f'Computing conviction score for {ticker}')

        try:
            conviction = compute_conviction(ticker, country=None)
        except Exception:
            conviction = 0.5

        _step(2, 'Groq agents (7)', f'Market Analyst → News Analyst → Bull/Bear × 4 → Evaluator → Trader')

        results = analyze_ondemand(
            [{'ticker': ticker, 'state': 'ondemand'}],
            {ticker: {'conviction': conviction}},
        )

        decision = results.get(ticker, {})
        action     = decision.get('action', 'WATCH')
        confidence = float(decision.get('confidence', 0.5))
        reasoning  = decision.get('reasoning', '')
        risk_notes = decision.get('risk_notes', '')

        _step(3, 'OpenRouter Risk Analyst', risk_notes[:200] if risk_notes else 'Risk assessment complete')
        _step(4, 'OpenRouter Portfolio Manager', f'{action} (conf={confidence:.0%})\n{reasoning[:200]}')

        save_recommendations({ticker: decision})

        blog('INFO', 'trading_agents',
             f'[ON-DEMAND] {ticker}: DONE → {action} ({confidence:.0%}). Refresh ticker page.')
    except Exception as e:
        from db.database import log as blog2
        blog2('ERROR', 'trading_agents', f'[ON-DEMAND] {ticker} failed: {e}')
    finally:
        with _analysis_lock:
            if ticker in _analysis_state['debate']:
                _analysis_state['debate'][ticker]['step_active'] = 0


@app.route('/analyze/<ticker>', methods=['POST'])
def analyze_ticker(ticker):
    """Queue on-demand analysis for a ticker (Groq + OpenRouter on-demand pool)."""
    ticker = ticker.upper().strip()

    # Check on-demand OR quota before queuing
    try:
        from analysis.rate_limit_manager import get_manager
        rate_mgr = get_manager()
        if not rate_mgr.can_run_ticker('ondemand'):
            st = rate_mgr.status()
            flash(
                f'On-demand quota exhausted for today '
                f'({st["or_ondemand_used"]}/{st["or_ondemand_used"] + st.get("or_ondemand_remaining", 0)} '
                f'OR calls used). Try again tomorrow.'
            )
            return redirect(url_for('ticker_detail', ticker=ticker))
    except Exception:
        pass

    with _analysis_lock:
        active  = _analysis_state['active']
        waiting = list(_analysis_state['queue'])

    if active == ticker or ticker in waiting:
        flash(f'{ticker} is already in the analysis queue — please wait.')
        return redirect(url_for('ticker_detail', ticker=ticker))

    with _analysis_lock:
        _analysis_state['queue'].append(ticker)
    _analysis_queue.put(ticker)

    position = len(_analysis_state['queue'])
    if active:
        flash(f'{ticker} added to queue (position {position}) — currently analyzing {active}. '
              f'Watch the Analysis Log for progress.')
    else:
        flash(f'Analysis started for {ticker} (~5 min, Groq + OpenRouter). Watch the Analysis Log below.')
    log('ta_wrapper', 'info', f'[ON-DEMAND] {ticker} queued (position {position}, active={active})')
    return redirect(url_for('ticker_detail', ticker=ticker))


# ── Universe Browse ───────────────────────────────────────────────────────────

_UNIVERSE = """
<div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
  <span class="page-header mb-0">Universe — {{ total }} stocks</span>
  <form method="GET" class="d-flex gap-2">
    <input name="q" value="{{ q }}" class="form-control form-control-sm" placeholder="Search ticker or company…" style="width:200px">
    <select name="country" class="form-select form-select-sm" style="width:120px">
      <option value="">All countries</option>
      {% for c in countries %}<option value="{{ c }}" {{ 'selected' if c==country_filter }}>{{ c }}</option>{% endfor %}
    </select>
    <select name="sector" class="form-select form-select-sm" style="width:160px">
      <option value="">All sectors</option>
      {% for s in sectors %}<option value="{{ s }}" {{ 'selected' if s==sector_filter }}>{{ s }}</option>{% endfor %}
    </select>
    <button class="btn btn-sm btn-outline-secondary">Filter</button>
  </form>
</div>

<div class="card">
  <div class="card-body p-0">
    <div class="table-responsive" style="max-height:75vh;overflow-y:auto">
    <table class="table table-sm mb-0">
      <thead style="position:sticky;top:0;background:var(--surface)">
        <tr><th>Ticker</th><th>Company</th><th>Sector</th><th>Country</th><th>Exchange</th><th>Actions</th></tr>
      </thead>
      <tbody>
      {% for u in stocks %}
      <tr>
        <td><a href="/ticker/{{ u.ticker }}" style="font-weight:700;color:var(--accent);text-decoration:none">{{ u.ticker }}</a></td>
        <td style="font-size:.82rem">{{ u.company_name or '—' }}</td>
        <td style="font-size:.78rem;color:var(--muted)">{{ u.sector or '—' }}</td>
        <td><span class="badge bg-secondary" style="font-size:.65rem">{{ u.country or '—' }}</span></td>
        <td style="font-size:.75rem;color:var(--muted)">{{ u.exchange or '—' }}</td>
        <td>
          <div class="d-flex gap-1">
            <form method="post" action="/watchlist/add" style="display:inline">
              <input type="hidden" name="ticker" value="{{ u.ticker }}">
              <input type="hidden" name="company_name" value="{{ u.company_name or u.ticker }}">
              <button class="btn btn-outline-warning btn-sm py-0 px-2" style="font-size:.7rem">+ Watch</button>
            </form>
            <form method="post" action="/analyze/{{ u.ticker }}" style="display:inline"
                  onsubmit="var b=this.querySelector('button');b.textContent='…';b.disabled=true;">
              <button type="submit" class="btn btn-outline-secondary btn-sm py-0 px-2" style="font-size:.7rem">Analyze</button>
            </form>
          </div>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="text-center text-muted py-4">No stocks found</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
</div>
{% if total > 100 %}
<div style="color:var(--muted);font-size:.78rem;margin-top:.5rem">
  Showing {{ stocks|length }} of {{ total }} — use filters to narrow down.
</div>
{% endif %}
"""


@app.route('/universe')
def universe_browse():
    q = request.args.get('q', '').strip()
    country_filter = request.args.get('country', '').strip()
    sector_filter = request.args.get('sector', '').strip()

    where = ['active=TRUE']
    params = []
    if q:
        where.append("(ticker ILIKE %s OR company_name ILIKE %s)")
        params += [f'%{q}%', f'%{q}%']
    if country_filter:
        where.append("country=%s")
        params.append(country_filter)
    if sector_filter:
        where.append("sector=%s")
        params.append(sector_filter)

    where_sql = ' AND '.join(where)
    stocks = query(f"""
        SELECT ticker, company_name, sector, country, exchange
        FROM universe WHERE {where_sql}
        ORDER BY ticker LIMIT 200
    """, params if params else None)

    total_row = query(f"SELECT COUNT(*) AS cnt FROM universe WHERE {where_sql}",
                      params if params else None)
    total = int(total_row[0]['cnt']) if total_row else 0

    countries = [r['country'] for r in
                 query("SELECT DISTINCT country FROM universe WHERE active=TRUE AND country IS NOT NULL ORDER BY country")]
    sectors = [r['sector'] for r in
               query("SELECT DISTINCT sector FROM universe WHERE active=TRUE AND sector IS NOT NULL ORDER BY sector")]

    return render_template_string(
        make_page(_RESEARCH_SUBNAV + _UNIVERSE, 'Research — Universe'),
        current_page='Research', research_page='universe',
        stocks=stocks, total=total, q=q,
        country_filter=country_filter, sector_filter=sector_filter,
        countries=countries, sectors=sectors,
        health=get_system_health(),
    )


_PORTFOLIO = """
<div class="d-flex justify-content-between align-items-center mb-3">
  <span class="page-header mb-0">Portfolio</span>
  <small style="color:var(--muted);font-size:.78rem">Current holdings &amp; performance</small>
</div>

<!-- Summary metrics -->
<div class="row g-3 mb-4">
  <div class="col-6 col-md-3">
    <div class="card h-100">
      <div class="card-body metric-card">
        <div class="metric-value">€{{ "%.0f"|format(total_value) }}</div>
        <div class="metric-label">Portfolio Value</div>
      </div>
    </div>
  </div>
  <div class="col-6 col-md-3">
    <div class="card h-100">
      <div class="card-body metric-card">
        <div class="metric-value">€{{ "%.0f"|format(total_cost) }}</div>
        <div class="metric-label">Total Cost</div>
      </div>
    </div>
  </div>
  <div class="col-6 col-md-3">
    <div class="card h-100">
      <div class="card-body metric-card">
        <div class="metric-value {{ 'text-success' if total_pnl >= 0 else 'text-danger' }}">
          {{ '+' if total_pnl >= 0 else '' }}€{{ "%.0f"|format(total_pnl) }}
        </div>
        <div class="metric-label">Unrealised P&amp;L ({{ '+' if total_pnl_pct >= 0 else '' }}{{ "%.1f"|format(total_pnl_pct) }}%)</div>
      </div>
    </div>
  </div>
  <div class="col-6 col-md-3">
    <div class="card h-100">
      <div class="card-body metric-card">
        <div class="metric-value">€{{ "%.0f"|format(cash) }}</div>
        <div class="metric-label">Available Cash</div>
      </div>
    </div>
  </div>
</div>

<!-- Holdings table -->
<div class="card mb-4">
  <div class="card-header d-flex justify-content-between">
    <span>Holdings ({{ holdings|length }})</span>
    {% if holdings %}
    <span style="color:var(--muted);font-size:.76rem;font-weight:400">Total: €{{ "%.0f"|format(total_value) }} invested + €{{ "%.0f"|format(cash) }} cash = €{{ "%.0f"|format(total_value + cash) }}</span>
    {% endif %}
  </div>
  <div class="card-body p-0">
    <table class="table table-hover mb-0">
      <thead>
        <tr>
          <th>Ticker</th>
          <th>Company</th>
          <th>Sector</th>
          <th>Region</th>
          <th class="text-end">Shares</th>
          <th class="text-end">Avg Buy</th>
          <th class="text-end">Current</th>
          <th class="text-end">Value (EUR)</th>
          <th class="text-end">P&amp;L</th>
        </tr>
      </thead>
      <tbody>
        {% for h in holdings | sort(attribute='curr_val', reverse=True) %}
        <tr>
          <td><a href="/ticker/{{ h.ticker }}" style="font-weight:600;color:var(--accent)">{{ h.ticker }}</a></td>
          <td style="color:var(--sub);font-size:.84rem">{{ (h.company or h.ticker)[:28] }}</td>
          <td><span style="font-size:.76rem;color:var(--muted)">{{ h.sector }}</span></td>
          <td><span style="font-size:.76rem;color:var(--muted)">{{ h.region }}</span></td>
          <td class="text-end" style="font-size:.84rem">{{ "%.4g"|format(h.shares) }}</td>
          <td class="text-end" style="color:var(--sub);font-size:.84rem">{{ h.currency }} {{ "%.2f"|format(h.avg_buy) }}</td>
          <td class="text-end" style="font-size:.84rem">{{ h.currency }} {{ "%.2f"|format(h.curr_price) }}</td>
          <td class="text-end" style="font-weight:600">€{{ "%.2f"|format(h.curr_val) }}</td>
          <td class="text-end" style="font-weight:600">
            <span class="{{ 'text-success' if h.pnl_pct >= 0 else 'text-danger' }}">
              {{ '+' if h.pnl_pct >= 0 else '' }}{{ "%.1f"|format(h.pnl_pct) }}%
            </span>
            <br><small style="color:var(--muted);font-weight:400">{{ '+' if h.pnl >= 0 else '' }}€{{ "%.0f"|format(h.pnl) }}</small>
          </td>
        </tr>
        {% else %}
        <tr>
          <td colspan="9" style="text-align:center;color:var(--muted);padding:2.5rem">
            No holdings yet — run the pipeline and execute BUY recommendations to start.
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<!-- Chart + breakdown row -->
<div class="row g-3">
  <div class="col-12 col-lg-8">
    <div class="card">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span>Portfolio Value History</span>
        <small style="color:var(--muted);font-size:.74rem;font-weight:400">Based on current holdings × historical prices</small>
      </div>
      <div class="card-body">
        {% if history_json != '[]' %}
        <div style="position:relative;height:240px">
          <canvas id="portfolioChart"></canvas>
        </div>
        {% else %}
        <p style="text-align:center;color:var(--muted);padding:2rem 0;font-size:.84rem">
          No price history available yet — prices populate after the first data collection run.
        </p>
        {% endif %}
      </div>
    </div>
  </div>
  <div class="col-12 col-lg-4">
    <div class="card mb-3">
      <div class="card-header">Sector Breakdown <small style="color:var(--muted);font-weight:400;font-size:.72rem">click to drill down</small></div>
      <div class="card-body" id="sectorBreakdown">
        {% for sec, pct in sector_breakdown | dictsort(by='value', reverse=True) %}
        <div class="breakdown-row" data-group="sector" data-key="{{ sec }}"
             style="margin-bottom:.5rem;cursor:pointer;border-radius:6px;padding:.3rem .4rem;transition:background .15s"
             onmouseenter="if(!this.classList.contains('bd-active'))this.style.background='rgba(79,142,247,.07)'"
             onmouseleave="if(!this.classList.contains('bd-active'))this.style.background=''"
             onclick="toggleDrill(this)">
          <div style="display:flex;justify-content:space-between;margin-bottom:.25rem;align-items:center">
            <span style="font-size:.8rem">{{ sec }}</span>
            <span style="font-size:.8rem;color:var(--sub)">{{ "%.1f"|format(pct) }}%
              <span class="drill-caret" style="font-size:.6rem;margin-left:.3rem;color:var(--muted)">▼</span>
            </span>
          </div>
          <div class="progress" style="height:4px;margin-bottom:0">
            <div class="progress-bar" style="width:{{ pct }}%;background:var(--accent)"></div>
          </div>
          <div class="drill-panel" style="display:none"></div>
        </div>
        {% else %}
        <p style="color:var(--muted);font-size:.82rem;margin:0">No holdings</p>
        {% endfor %}
      </div>
    </div>
    <div class="card">
      <div class="card-header">Region Breakdown <small style="color:var(--muted);font-weight:400;font-size:.72rem">click to drill down</small></div>
      <div class="card-body" id="regionBreakdown">
        {% for reg, pct in region_breakdown | dictsort(by='value', reverse=True) %}
        <div class="breakdown-row" data-group="region" data-key="{{ reg }}"
             style="margin-bottom:.5rem;cursor:pointer;border-radius:6px;padding:.3rem .4rem;transition:background .15s"
             onmouseenter="if(!this.classList.contains('bd-active'))this.style.background='rgba(79,142,247,.07)'"
             onmouseleave="if(!this.classList.contains('bd-active'))this.style.background=''"
             onclick="toggleDrill(this)">
          <div style="display:flex;justify-content:space-between;margin-bottom:.25rem;align-items:center">
            <span style="font-size:.8rem">{{ reg }}</span>
            <span style="font-size:.8rem;color:var(--sub)">{{ "%.1f"|format(pct) }}%
              <span class="drill-caret" style="font-size:.6rem;margin-left:.3rem;color:var(--muted)">▼</span>
            </span>
          </div>
          <div class="progress" style="height:4px;margin-bottom:0">
            <div class="progress-bar" style="width:{{ pct }}%;background:var(--green)"></div>
          </div>
          <div class="drill-panel" style="display:none"></div>
        </div>
        {% else %}
        <p style="color:var(--muted);font-size:.82rem;margin:0">No holdings</p>
        {% endfor %}
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
var _hld = {{ holdings_json | safe }};

function toggleDrill(row) {
  var group = row.dataset.group;
  var key   = row.dataset.key;
  var panel = row.querySelector('.drill-panel');
  var caret = row.querySelector('.drill-caret');
  var open  = row.classList.contains('bd-active');

  // Close other open rows in same card
  row.closest('.card-body').querySelectorAll('.breakdown-row.bd-active').forEach(function(r) {
    if (r !== row) {
      r.classList.remove('bd-active');
      r.style.background = '';
      r.querySelector('.drill-panel').style.display = 'none';
      r.querySelector('.drill-panel').innerHTML = '';
      r.querySelector('.drill-caret').textContent = '▼';
    }
  });

  if (open) {
    row.classList.remove('bd-active');
    row.style.background = '';
    panel.style.display = 'none';
    panel.innerHTML = '';
    caret.textContent = '▼';
    return;
  }

  var items = _hld.filter(function(h){ return h[group] === key; });
  items.sort(function(a,b){ return b.curr_val - a.curr_val; });

  if (!items.length) {
    panel.innerHTML = '<p style="font-size:.75rem;color:var(--muted);margin:.5rem 0 0">No holdings data</p>';
  } else {
    var trs = items.map(function(h){
      var pc = h.pnl_pct >= 0 ? 'var(--green)' : 'var(--red)';
      var ps = h.pnl_pct >= 0 ? '+' : '';
      var co = h.company.length > 20 ? h.company.slice(0,19)+'…' : h.company;
      return '<tr>'
        +'<td style="padding:.18rem .3rem"><a href="/ticker/'+h.ticker+'" style="color:var(--accent);font-weight:600;text-decoration:none">'+h.ticker+'</a></td>'
        +'<td style="padding:.18rem .3rem;color:var(--sub)">'+co+'</td>'
        +'<td style="padding:.18rem .3rem;text-align:right">'+h.shares.toLocaleString('fi-FI',{maximumFractionDigits:1})+'</td>'
        +'<td style="padding:.18rem .3rem;text-align:right">€'+h.curr_val.toLocaleString('fi-FI',{maximumFractionDigits:0})+'</td>'
        +'<td style="padding:.18rem .3rem;text-align:right;color:'+pc+'">'+ps+h.pnl_pct.toFixed(1)+'%</td>'
        +'</tr>';
    }).join('');
    panel.innerHTML = '<div style="margin-top:.55rem;border-top:1px solid rgba(255,255,255,.06);padding-top:.5rem">'
      +'<table style="width:100%;border-collapse:collapse;font-size:.74rem">'
      +'<thead><tr style="color:var(--muted);font-size:.7rem">'
      +'<th style="padding:.1rem .3rem;font-weight:400">Ticker</th>'
      +'<th style="font-weight:400">Company</th>'
      +'<th style="text-align:right;font-weight:400">Shares</th>'
      +'<th style="text-align:right;font-weight:400">Value</th>'
      +'<th style="text-align:right;font-weight:400">P&amp;L</th>'
      +'</tr></thead><tbody>'+trs+'</tbody></table></div>';
  }

  panel.style.display = 'block';
  row.classList.add('bd-active');
  row.style.background = 'rgba(79,142,247,.1)';
  caret.textContent = '▲';
}

(function(){
  var raw = {{ history_json | safe }};
  var canvas = document.getElementById('portfolioChart');
  if (!canvas || !raw.length) return;
  new Chart(canvas, {
    type: 'line',
    data: {
      labels: raw.map(function(d){ return d.date; }),
      datasets: [{
        data: raw.map(function(d){ return d.value; }),
        borderColor: '#4f8ef7',
        backgroundColor: 'rgba(79,142,247,0.07)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: {
        callbacks: {
          label: function(ctx){ return ' €' + ctx.parsed.y.toLocaleString('fi-FI', {maximumFractionDigits:0}); }
        }
      }},
      scales: {
        x: {
          grid: { color: '#232f48' },
          ticks: { color: '#9aa5be', font: { size: 10 }, maxTicksLimit: 8 }
        },
        y: {
          grid: { color: '#232f48' },
          ticks: {
            color: '#9aa5be', font: { size: 10 },
            callback: function(v){ return '€' + v.toLocaleString('fi-FI', {maximumFractionDigits:0}); }
          }
        }
      }
    }
  });
})();
</script>
"""


@app.route('/portfolio')
def portfolio():
    import json as _json
    from portfolio.sector_utils import country_to_region, normalize as _normalize_sector

    holdings, total_value = get_holdings_enriched()
    cash = get_cash()
    fx = get_live_fx()

    # Enrich each holding with region
    for h in holdings:
        uni = query("SELECT country FROM universe WHERE ticker=%s LIMIT 1", (h['ticker'],))
        country = (uni[0]['country'] if uni else 'US') or 'US'
        h['region'] = country_to_region(country)
        h['sector'] = _normalize_sector(h['sector'] or 'Unknown')

    # P&L totals — use per-holding pnl already computed by get_holdings_enriched()
    # with a consistent FX rate, rather than a second fx call that can diverge.
    total_pnl = sum(h['pnl'] for h in holdings)
    total_cost = total_value - total_pnl
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0

    # Sector & region breakdown (% of portfolio value)
    sector_vals, region_vals = {}, {}
    for h in holdings:
        sector_vals[h['sector']] = sector_vals.get(h['sector'], 0) + h['curr_val']
        region_vals[h['region']] = region_vals.get(h['region'], 0) + h['curr_val']
    sector_breakdown = {s: round(v / total_value * 100, 1) for s, v in sector_vals.items()} if total_value else {}
    region_breakdown = {r: round(v / total_value * 100, 1) for r, v in region_vals.items()} if total_value else {}

    history = _get_portfolio_history(holdings, fx)

    # Serialize holdings for JS drill-down (sector/region clickable breakdown)
    holdings_js = _json.dumps([
        {
            'ticker':   h['ticker'],
            'company':  h.get('company') or h['ticker'],
            'shares':   round(h['shares'], 4),
            'curr_val': round(h['curr_val'], 2),
            'pnl_pct':  round(h['pnl_pct'], 2),
            'sector':   h['sector'],
            'region':   h['region'],
        }
        for h in holdings
    ])

    return render_template_string(
        make_page(_PORTFOLIO, 'Portfolio'),
        current_page='Portfolio',
        holdings=holdings,
        total_value=total_value, total_cost=total_cost,
        total_pnl=total_pnl, total_pnl_pct=total_pnl_pct,
        cash=cash,
        sector_breakdown=sector_breakdown,
        region_breakdown=region_breakdown,
        history_json=_json.dumps(history),
        holdings_json=holdings_js,
        health=get_system_health(),
    )


if __name__ == '__main__':
    import time as _time
    port = int(os.environ.get('DASHBOARD_PORT', 5000))
    for attempt in range(1, 6):
        try:
            log('INFO', 'dashboard', f'Starting web dashboard on 0.0.0.0:{port} (attempt {attempt})')
            app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
            break
        except OSError as e:
            if 'Address already in use' in str(e):
                log('WARNING', 'dashboard', f'Port {port} in use — waiting 10s before retry')
                _time.sleep(10)
            else:
                raise
