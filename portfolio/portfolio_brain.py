"""
Portfolio Brain — unified rebalancing and recommendation engine.

Logic flow per run:
  1. Load holdings, sector targets, prices, TradingAgents recs, discovery candidates
  2. Score market regime (VIX, yield curve, credit spreads, GDELT) → BULLISH/NEUTRAL/BEARISH
  3. For each holding: stop-loss, TA SELL, sector rotation
  4. For each underweight sector: find best candidate
  5. Trending overrides (high-score signals)
  6. Per-buy conviction gate: require multi-source agreement before spending cash
  7. Size positions with regime-adjusted multiplier
  8. Save recommendations, return brief
"""
import os
import sys
import math
import json
from datetime import date, timedelta

sys.path.insert(0, '/home/ubuntu/advisor')
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')
from db.database import query, execute, log
from portfolio.optimizer import optimize_candidates, get_weight_hint
from portfolio.sector_utils import normalize as normalize_sector, country_to_region, DEFAULT_REGION_TARGETS

MIN_TRADE_VALUE_EUR = float(os.getenv('MIN_TRADE_VALUE_EUR', 50))


# ── Settings helpers ──────────────────────────────────────────────────────────

def _setting(key, default):
    rows = query("SELECT value FROM user_settings WHERE key=%s", (key,))
    if not rows or not rows[0]['value']:
        return default
    try:
        return type(default)(rows[0]['value'])
    except (ValueError, TypeError):
        return rows[0]['value']


def _sector_targets():
    row = query("SELECT value FROM user_settings WHERE key='sector_targets'")
    if row and row[0]['value']:
        try:
            return json.loads(row[0]['value'])
        except Exception:
            pass
    return {}


# ── FX ────────────────────────────────────────────────────────────────────────

_FX_CACHE = {}

def _fx():
    global _FX_CACHE
    if _FX_CACHE:
        return _FX_CACHE
    static = {'EUR': 1.0, 'USD': 0.92, 'GBP': 1.17, 'JPY': 0.0063,
              'HKD': 0.12, 'AUD': 0.59, 'SEK': 0.088, 'CHF': 1.04}
    try:
        rows = query("""
            SELECT series_id, value FROM macro_data
            WHERE series_id IN ('DEXUSEU','DEXUSUK','DEXJPUS','DEXUSAL',
                                'DEXSDUS','DEXSZUS','DEXHKUS')
            AND date >= CURRENT_DATE - INTERVAL '30 days'
            ORDER BY series_id, date DESC
        """)
        seen = {}
        for r in rows:
            if r['series_id'] not in seen and r['value']:
                seen[r['series_id']] = float(r['value'])
        if seen:
            usd_eur = seen.get('DEXUSEU', 1.09)
            eur_usd = 1.0 / usd_eur
            _FX_CACHE = {
                'EUR': 1.0, 'USD': eur_usd,
                'GBP': seen.get('DEXUSUK', 1.37) / usd_eur,
                'JPY': eur_usd / seen['DEXJPUS'] if 'DEXJPUS' in seen else 0.0063,
                'AUD': eur_usd / seen['DEXUSAL'] if 'DEXUSAL' in seen else 0.59,
                'SEK': eur_usd / seen['DEXSDUS'] if 'DEXSDUS' in seen else 0.078,
                'CHF': eur_usd / seen['DEXSZUS'] if 'DEXSZUS' in seen else 1.06,
                'HKD': eur_usd / seen['DEXHKUS'] if 'DEXHKUS' in seen else 0.11,
            }
            return _FX_CACHE
    except Exception as e:
        log('WARNING', 'portfolio_brain', f'FX lookup failed: {e}')
    _FX_CACHE = static
    return static


def _market_regime():
    """
    Score overall market conditions 0–100 (0=extremely bearish, 50=neutral, 100=bullish).
    Uses VIX, yield curve, credit spreads, Fed rate, GDELT global tone, Polymarket macro.

    Returns dict: {score, regime, confidence_multiplier, size_multiplier, notes}
    """
    score = 50  # start neutral
    notes = []

    try:
        def _macro(sid):
            r = query("""SELECT value FROM macro_data WHERE series_id=%s
                         AND date >= CURRENT_DATE - INTERVAL '30 days'
                         ORDER BY date DESC LIMIT 1""", (sid,))
            return float(r[0]['value']) if r and r[0]['value'] else None

        vix       = _macro('VIXCLS')
        t10y2y    = _macro('T10Y2Y')
        hy_spread = _macro('BAMLH0A0HYM2')
        fed_rate  = _macro('DFF')
        dgs10     = _macro('DGS10')
        t5yifr    = _macro('T5YIFR')

        # ── VIX (fear gauge) ──────────────────────────────────────────────
        if vix is not None:
            if vix < 15:
                score += 10; notes.append(f'VIX={vix:.1f} (calm)')
            elif vix < 20:
                score += 5;  notes.append(f'VIX={vix:.1f} (low)')
            elif vix < 25:
                pass;        notes.append(f'VIX={vix:.1f} (neutral)')
            elif vix < 35:
                score -= 10; notes.append(f'VIX={vix:.1f} (elevated)')
            else:
                score -= 20; notes.append(f'VIX={vix:.1f} ⚠ HIGH FEAR')

        # ── Yield curve (T10Y-T2Y) ────────────────────────────────────────
        if t10y2y is not None:
            if t10y2y > 0.5:
                score += 8;  notes.append(f'Yield curve +{t10y2y:.2f} (healthy)')
            elif t10y2y > 0:
                score += 3;  notes.append(f'Yield curve +{t10y2y:.2f} (flat)')
            elif t10y2y > -0.3:
                score -= 5;  notes.append(f'Yield curve {t10y2y:.2f} (slightly inverted)')
            else:
                score -= 15; notes.append(f'Yield curve {t10y2y:.2f} ⚠ INVERTED')

        # ── High-yield credit spread (risk appetite) ──────────────────────
        if hy_spread is not None:
            if hy_spread < 3.0:
                score += 8;  notes.append(f'HY spread {hy_spread:.2f}% (risk-on)')
            elif hy_spread < 4.5:
                score += 2;  notes.append(f'HY spread {hy_spread:.2f}% (normal)')
            elif hy_spread < 6.0:
                score -= 8;  notes.append(f'HY spread {hy_spread:.2f}% (stress)')
            else:
                score -= 18; notes.append(f'HY spread {hy_spread:.2f}% ⚠ CREDIT STRESS')

        # ── Fed rate (policy tightness) ───────────────────────────────────
        if fed_rate is not None:
            if fed_rate > 5.0:
                score -= 10; notes.append(f'Fed rate {fed_rate:.2f}% (very restrictive)')
            elif fed_rate > 4.0:
                score -= 5;  notes.append(f'Fed rate {fed_rate:.2f}% (restrictive)')
            elif fed_rate < 2.0:
                score += 8;  notes.append(f'Fed rate {fed_rate:.2f}% (accommodative)')
            else:
                notes.append(f'Fed rate {fed_rate:.2f}% (neutral)')

        # ── 5Y forward inflation (inflation anchoring) ────────────────────
        if t5yifr is not None:
            if t5yifr < 2.5:
                score += 5;  notes.append(f'5Y fwd inflation {t5yifr:.2f}% (anchored)')
            elif t5yifr > 3.5:
                score -= 8;  notes.append(f'5Y fwd inflation {t5yifr:.2f}% ⚠ UNANCHORED')

    except Exception as e:
        log('WARNING', 'portfolio_brain', f'Macro regime check error: {e}')

    # ── GDELT global sentiment ────────────────────────────────────────────
    try:
        gdelt_rows = query("""
            SELECT AVG(avg_tone) AS tone, AVG(mention_velocity) AS vel
            FROM news_sentiment
            WHERE date >= CURRENT_DATE - INTERVAL '7 days'
        """)
        if gdelt_rows and gdelt_rows[0]['tone'] is not None:
            tone = float(gdelt_rows[0]['tone'])
            vel  = float(gdelt_rows[0]['vel'] or 1)
            if tone > 1.0:
                score += 5; notes.append(f'GDELT tone +{tone:.2f} (positive sentiment)')
            elif tone < -1.5:
                score -= 8; notes.append(f'GDELT tone {tone:.2f} ⚠ negative sentiment')
            if vel > 2.0:
                score -= 3; notes.append(f'GDELT velocity {vel:.1f}x (elevated news flow)')
    except Exception:
        pass

    # ── Polymarket macro signals ──────────────────────────────────────────
    try:
        macro_poly = query("""
            SELECT question, probability FROM polymarket_signals
            WHERE (question ILIKE '%recession%' OR question ILIKE '%fed cut%'
                   OR question ILIKE '%rate hike%' OR question ILIKE '%market crash%')
            AND date >= CURRENT_DATE - INTERVAL '7 days'
            ORDER BY date DESC LIMIT 5
        """)
        for p in macro_poly:
            prob = float(p['probability'])
            q    = p['question'].lower()
            if 'recession' in q or 'crash' in q:
                if prob > 0.4:
                    score -= int(prob * 12)
                    notes.append(f'Polymarket: {p["question"][:50]} {prob:.0%}')
            elif 'rate cut' in q or 'fed cut' in q:
                if prob > 0.5:
                    score += int(prob * 6)
                    notes.append(f'Polymarket: {p["question"][:50]} {prob:.0%}')
    except Exception:
        pass

    score = max(0, min(100, score))

    if score >= 60:
        regime = 'BULLISH'
        conf_mult = 1.0       # normal confidence threshold
        size_mult = 1.0       # full position sizes
    elif score >= 40:
        regime = 'NEUTRAL'
        conf_mult = 1.05      # slightly tighter (×1.05 means +5% on threshold)
        size_mult = 0.85      # 85% of calculated sizes
    elif score >= 25:
        regime = 'BEARISH'
        conf_mult = 1.15      # need 15% higher confidence to buy
        size_mult = 0.5       # half-sized positions
    else:
        regime = 'RISK-OFF'
        conf_mult = 1.30      # need 30% higher confidence — market very stressed
        size_mult = 0.25      # very small positions; preserve cash

    log('INFO', 'portfolio_brain',
        f'Market regime: {regime} (score={score}/100) | {"; ".join(notes[:4])}')

    return {
        'score':        score,
        'regime':       regime,
        'conf_mult':    conf_mult,
        'size_mult':    size_mult,
        'notes':        notes,
        'summary':      f'{regime} ({score}/100): ' + '; '.join(notes[:3]),
    }


def _conviction_check(ticker, ta_recs, disc_by_ticker):
    """
    Return (passes, sources_list) — passes=True if at least 2 independent
    signals agree on BUY for this ticker.
    """
    sources = []
    ta = ta_recs.get(ticker, {})
    ta_action = ta.get('action', '')
    ta_conf = float(ta.get('confidence') or 0)
    if ta_action == 'BUY' and ta_conf >= 0.55:
        sources.append(f"TradingAgents({ta_conf:.0%})")
        # High-confidence TA runs 6 analysts internally — counts as 2 independent signals
        if ta_conf >= 0.70:
            sources.append('TradingAgents(multi-analyst)')
    elif ta_action == 'WATCH' and ta_conf >= 0.60:
        # WATCH = analyzed and not sold → weak positive signal (counts as 1)
        sources.append(f"TradingAgents-WATCH({ta_conf:.0%})")

    disc = disc_by_ticker.get(ticker)
    if disc and disc.get('direction') == 'BUY':
        score = float(disc.get('blended_score') or disc.get('total_score', 0))
        if score >= 40:
            sources.append(f"Discovery(score={score:.0f})")

    gdelt = _gdelt_signal(ticker)
    if gdelt.get('is_accelerating') and float(gdelt.get('mention_velocity') or 1) > 1.5:
        sources.append(f"GDELT(vel={float(gdelt.get('mention_velocity',1)):.1f}x)")

    poly = _polymarket_signal(ticker)
    if poly and float(poly.get('probability') or 0) > 0.6:
        sources.append(f"Polymarket({float(poly['probability']):.0%})")

    return len(sources) >= 2, sources


def _price_eur(ticker, currency):
    fx = _fx()
    rows = query("SELECT close FROM prices WHERE ticker=%s ORDER BY date DESC LIMIT 1", (ticker,))
    if not rows:
        return None
    price = float(rows[0]['close'])
    rate = fx.get(currency or 'USD', fx.get('USD', 0.92))
    return round(price * rate, 4)


# ── Holdings with enrichment ──────────────────────────────────────────────────

def _load_holdings():
    """Return holdings with current price, P&L, sector."""
    rows = query("""
        SELECT h.ticker, h.shares, h.avg_buy_price, h.currency,
               COALESCE(u.sector, 'Unknown') AS sector,
               COALESCE(u.company_name, h.ticker) AS company_name
        FROM holdings h
        LEFT JOIN universe u ON h.ticker = u.ticker
        WHERE h.active = TRUE
    """)
    enriched = []
    for h in rows:
        shares = float(h['shares'])
        avg = float(h['avg_buy_price'] or 0)
        ccy = h['currency'] or 'USD'
        cur_price_eur = _price_eur(h['ticker'], ccy)
        avg_eur = avg * _fx().get(ccy, _fx().get('USD', 0.92))
        pnl_pct = ((cur_price_eur - avg_eur) / avg_eur * 100) if avg_eur and cur_price_eur else 0.0
        value_eur = (cur_price_eur or avg_eur) * shares
        enriched.append({
            **h,
            'shares': shares,
            'avg_buy_price': avg,
            'cur_price_eur': cur_price_eur,
            'avg_eur': avg_eur,
            'pnl_pct': round(pnl_pct, 2),
            'value_eur': round(value_eur, 2),
        })
    return enriched


def _region_targets() -> dict:
    row = query("SELECT value FROM user_settings WHERE key='region_targets'")
    if row and row[0]['value']:
        try:
            import json as _j
            return _j.loads(row[0]['value'])
        except Exception:
            pass
    return DEFAULT_REGION_TARGETS.copy()


def _sector_exposure(holdings):
    """Return {canonical_sector: pct_of_portfolio} from current holdings."""
    total = sum(h['value_eur'] for h in holdings)
    if not total:
        return {}
    exposure = {}
    for h in holdings:
        s = normalize_sector(h['sector'])
        exposure[s] = exposure.get(s, 0) + h['value_eur']
    return {s: round(v / total * 100, 1) for s, v in exposure.items()}


def _region_exposure(holdings):
    """Return {region: pct_of_portfolio} from current holdings."""
    total = sum(h['value_eur'] for h in holdings)
    if not total:
        return {}
    tickers = [h['ticker'] for h in holdings]
    rows = query("SELECT ticker, country FROM universe WHERE ticker = ANY(%s)", (tickers,))
    country_map = {r['ticker']: r['country'] for r in rows}
    region_vals = {}
    for h in holdings:
        country = country_map.get(h['ticker'], 'US') or 'US'
        region = country_to_region(country)
        region_vals[region] = region_vals.get(region, 0) + h['value_eur']
    return {r: round(v / total * 100, 1) for r, v in sorted(region_vals.items(), key=lambda x: -x[1])}


# ── Signal data ───────────────────────────────────────────────────────────────

def _ta_recs_today():
    """TradingAgents recommendations for today keyed by ticker."""
    rows = query("""
        SELECT DISTINCT ON (ticker) ticker, action, confidence, reasoning,
               signal_sources, bull_case, bear_case, key_risks
        FROM recommendations
        WHERE date = %s
        ORDER BY ticker, confidence DESC
    """, (date.today(),))
    return {r['ticker']: r for r in rows}


def _discovery_today():
    """Top eligible discovery candidates today, deduplicated by ticker."""
    rows = query("""
        SELECT * FROM (
            SELECT DISTINCT ON (dc.ticker)
                   dc.ticker, dc.direction, dc.event_type, dc.total_score,
                   dc.blended_score, dc.gdelt_score, dc.reason,
                   dc.eligible, dc.tax_notes, dc.currency, dc.fx_risk,
                   COALESCE(u.sector, '') AS sector,
                   COALESCE(u.company_name, dc.ticker) AS company_name
            FROM discovery_candidates dc
            LEFT JOIN universe u ON dc.ticker = u.ticker
            WHERE dc.date = %s AND dc.eligible = TRUE
            ORDER BY dc.ticker, dc.total_score DESC
        ) sub
        ORDER BY COALESCE(blended_score, total_score) DESC
    """, (date.today(),))
    return rows


def _gdelt_signal(ticker):
    row = query("""
        SELECT mention_velocity, avg_tone, is_accelerating
        FROM news_sentiment WHERE ticker=%s ORDER BY date DESC LIMIT 1
    """, (ticker,))
    return row[0] if row else {}


def _polymarket_signal(ticker):
    rows = query("""
        SELECT question, probability FROM polymarket_signals
        WHERE relevant_tickers LIKE %s
          AND date >= CURRENT_DATE - INTERVAL '3 days'
        ORDER BY date DESC, probability DESC LIMIT 1
    """, (f'%{ticker}%',))
    return rows[0] if rows else {}


# ── Sizing ────────────────────────────────────────────────────────────────────

def _kelly_fraction(ticker):
    row = query("""
        SELECT win_rate, sharpe_ratio FROM backtest_results
        WHERE ticker=%s ORDER BY created_at DESC LIMIT 1
    """, (ticker,))
    if row and row[0]['win_rate']:
        p = float(row[0]['win_rate'])
        b = max(1.0, float(row[0]['sharpe_ratio'] or 1.0))
        return min(max(0, (p * b - (1 - p)) / b) * 0.5, 0.35)
    return None


def _size_position(ticker, currency, available_eur, weight_hint=None, total_budget=None):
    """
    Return (shares, price_eur, total_cost_eur) or None.
    Position sizing logic (priority order):
      1. Optimizer weight_hint — use portfolio-optimal fraction of total_budget
      2. Kelly fraction — use Kelly-optimal fraction of available
      3. Default — 1/N equal weight across target_positions (≈10 stocks max)
    Never allocates more than 35% of total_budget in one position.
    """
    price_eur = _price_eur(ticker, currency or 'USD')
    if not price_eur or price_eur <= 0:
        # Price missing from DB — try live yfinance fetch as fallback
        try:
            import yfinance as yf
            from pipeline.price_collector import fetch_and_store_prices
            fetch_and_store_prices(ticker, days_back=7)
            price_eur = _price_eur(ticker, currency or 'USD')
        except Exception:
            pass
        if not price_eur or price_eur <= 0:
            log('WARNING', 'portfolio_brain',
                f'{ticker}: no price data — cannot size position')
            return None

    kelly = _kelly_fraction(ticker)
    base  = total_budget or available_eur

    if weight_hint is not None:
        # Clamp optimizer weight to 5%–35%; anything outside is either too tiny to matter
        # or dangerously concentrated
        w     = max(0.05, min(0.35, weight_hint))
        alloc = base * w
    elif kelly is not None:
        # Half-Kelly: theoretical Kelly is aggressive; halving it accounts for
        # model error and real-world liquidity constraints
        alloc = available_eur * min(kelly, 0.25)
    else:
        # Default equal-weight across ~10 positions, hard-capped at 15% of total
        alloc = min(available_eur * 0.12, base * 0.15)

    alloc = max(alloc, price_eur)  # at least 1 share
    shares = max(1, int(alloc / price_eur))
    cost = round(shares * price_eur, 2)

    # Hard cap: never spend more than available in this call
    if cost > available_eur:
        shares = max(1, int(available_eur / price_eur))
        cost = round(shares * price_eur, 2)
    if cost > available_eur:
        return None
    return shares, price_eur, cost


# ── Save recommendation ───────────────────────────────────────────────────────

def _save_rec(ticker, action, confidence, reasoning, sources, extra=None):
    extra = extra or {}
    today = date.today()
    # Preserve debate_trace from the trading_agents row before deleting it
    if not extra.get('debate_trace'):
        existing = query(
            "SELECT debate_trace FROM recommendations WHERE ticker=%s AND date=%s "
            "AND debate_trace IS NOT NULL AND length(debate_trace) > 10 "
            "ORDER BY created_at DESC LIMIT 1",
            (ticker, today)
        )
        if existing and existing[0]['debate_trace']:
            extra = {**extra, 'debate_trace': existing[0]['debate_trace']}
    execute("DELETE FROM recommendations WHERE ticker=%s AND date=%s", (ticker, today))
    execute("""
        INSERT INTO recommendations
            (ticker, date, action, confidence, reasoning, signal_sources,
             bull_case, bear_case, key_risks, time_horizon, debate_trace)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        ticker, today, action, confidence, reasoning, sources,
        extra.get('bull_case', ''), extra.get('bear_case', ''),
        extra.get('key_risks', ''), extra.get('time_horizon', 'medium-term'),
        extra.get('debate_trace', ''),
    ))


# ── Core rebalancing logic ────────────────────────────────────────────────────

def _watch_trigger(ticker, confidence):
    ns = _gdelt_signal(ticker)
    if ns and not ns.get('is_accelerating'):
        return 'Wait for GDELT velocity to exceed 2x baseline'
    if confidence < 0.5:
        return 'Wait for multi-source confirmation'
    return f'Confidence must reach 60% (currently {confidence:.0%})'


def _compose_reasoning(parts):
    return ' '.join(p for p in parts if p)


def build_brief():
    log('INFO', 'portfolio_brain', 'Building portfolio brief...')

    # ── Settings ──────────────────────────────────────────────────────────────
    cash_eur            = _setting('nordnet_cash_eur', 0.0)
    # Monthly budget: prefer simulation_monthly_deposit when simulation is enabled
    simulate_on = _setting('simulate_recommendations', 'false').lower() == 'true'
    if simulate_on:
        monthly_budget = _setting('simulation_monthly_deposit', _setting('monthly_investment_budget_eur', 500.0))
    else:
        monthly_budget  = _setting('monthly_investment_budget_eur', 500.0)

    # Monthly reset: clear monthly_invested_this_month if we're in a new month
    invested_month_key = 'monthly_invested_this_month'
    invested_year_month = query("SELECT value FROM user_settings WHERE key='monthly_invested_reset_ym'")
    current_ym = date.today().strftime('%Y-%m')
    stored_ym  = invested_year_month[0]['value'] if invested_year_month else ''
    if stored_ym != current_ym:
        execute("DELETE FROM user_settings WHERE key IN (%s, 'monthly_invested_reset_ym')",
                (invested_month_key,))
        execute("INSERT INTO user_settings (key, value) VALUES ('monthly_invested_reset_ym', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=%s", (current_ym, current_ym))
        log('INFO', 'portfolio_brain', f'Monthly invested counter reset for {current_ym}')

    monthly_invested    = _setting(invested_month_key, 0.0)
    min_confidence      = float(_setting('min_confidence', 0.6))
    stop_loss_pct       = _setting('stop_loss_pct', 15.0)
    rebalance_threshold = _setting('rebalance_threshold_pct', 7.0)
    trending_override   = _setting('trending_override_score', 75.0)
    remaining_monthly   = max(0, float(monthly_budget) - float(monthly_invested))
    log('INFO', 'portfolio_brain',
        f'Cash: €{float(cash_eur):.0f} | Monthly budget: €{float(monthly_budget):.0f} '
        f'| Invested this month: €{float(monthly_invested):.0f} | Deployable: €{remaining_monthly:.0f}')

    # ── Market regime ─────────────────────────────────────────────────────────
    regime = _market_regime()
    # Cap at 92%: in extreme stress the multiplier could push required confidence above 1.0,
    # which would block every buy forever. 92% is demanding but still reachable.
    effective_min_conf = min(0.92, min_confidence * regime['conf_mult'])
    size_mult          = regime['size_mult']

    if regime['regime'] in ('BEARISH', 'RISK-OFF'):
        log('WARNING', 'portfolio_brain',
            f'Regime {regime["regime"]} — raising buy threshold to '
            f'{effective_min_conf:.0%}, sizing at {size_mult:.0%}. '
            f'Cash preservation preferred.')

    targets         = _sector_targets()
    reg_targets     = _region_targets()
    holdings        = _load_holdings()
    held_map        = {h['ticker']: h for h in holdings}
    held_set        = set(held_map)
    exposure        = _sector_exposure(holdings)
    region_exposure = _region_exposure(holdings)
    ta_recs         = _ta_recs_today()
    discovery = _discovery_today()

    # Index discovery by ticker and sector (normalize sector names)
    disc_by_ticker = {d['ticker']: d for d in discovery}
    disc_by_sector = {}
    for d in discovery:
        s = normalize_sector(d.get('sector', ''))
        d['_sector_norm'] = s  # cache normalized sector on the dict
        if s and s != 'Unknown':
            disc_by_sector.setdefault(s, []).append(d)

    sells, buys, watches = [], [], []
    freed_eur = 0.0
    sell_reasons = {}   # ticker → reason string for logging

    # ── 1. Analyse existing holdings ─────────────────────────────────────────
    for h in holdings:
        ticker  = h['ticker']
        sector  = normalize_sector(h['sector'])
        pnl_pct = h['pnl_pct']
        val_eur = h['value_eur']
        ta      = ta_recs.get(ticker, {})
        ta_conf = float(ta.get('confidence') or 0)
        ta_act  = ta.get('action', 'HOLD')
        gdelt   = _gdelt_signal(ticker)
        velocity = float(gdelt.get('mention_velocity') or 1.0)
        tone     = float(gdelt.get('avg_tone') or 0)

        sell_signal = False
        sell_reason = ''
        shares_to_sell = float(h['shares'])
        partial = False

        # (a) Hard stop-loss
        if pnl_pct < -float(stop_loss_pct):
            sell_signal = True
            sell_reason = (f'Stop-loss triggered: position down {pnl_pct:.1f}% '
                           f'(threshold -{stop_loss_pct:.0f}%)')
            confidence  = 0.90

        # (b) TradingAgents SELL with confidence
        elif ta_act == 'SELL' and ta_conf >= float(min_confidence):
            sell_signal = True
            sell_reason = ta.get('reasoning') or f'TradingAgents SELL signal ({ta_conf:.0%})'
            confidence  = ta_conf
            if ta_conf < 0.8:
                shares_to_sell = round(float(h['shares']) * 0.5, 0)
                partial = True

        # (c) Sector overweight + weak fundamentals → rotation candidate
        elif targets:
            tgt = float(targets.get(sector, 0))
            cur = float(exposure.get(sector, 0))
            overweight = cur - tgt
            if overweight > float(rebalance_threshold):
                # Only sell if the stock also has a weak signal
                weak = (velocity < 0.8 or tone < -1.0 or
                        (ta_act == 'SELL' and ta_conf > 0.5))
                # Check if a better candidate exists in the same sector
                better = [d for d in disc_by_sector.get(sector, [])
                          if d['ticker'] != ticker
                          and d.get('direction') == 'BUY'
                          and (d.get('blended_score') or d.get('total_score', 0)) > 40]
                if weak and better:
                    sell_signal = True
                    best_alt = better[0]
                    sell_reason = (
                        f'Sector {sector} overweight by {overweight:.1f}% vs target {tgt:.0f}%. '
                        f'Rotating into {best_alt["ticker"]} '
                        f'(score {best_alt.get("total_score",0):.0f}, '
                        f'{best_alt.get("event_type","")}).'
                    )
                    confidence = min(0.75, 0.5 + overweight / 30)
                    shares_to_sell = round(float(h['shares']) * min(0.5, overweight / 100), 0)
                    shares_to_sell = max(1, int(shares_to_sell))
                    partial = True

        if sell_signal:
            price_eur = h['cur_price_eur'] or h['avg_eur']
            proceeds  = round(shares_to_sell * price_eur, 2)
            freed_eur += proceeds
            sells.append({
                'ticker':        ticker,
                'company_name':  h['company_name'],
                'sector':        sector,
                'action':        'SELL',
                'shares':        shares_to_sell,
                'shares_held':   float(h['shares']),
                'price_eur':     price_eur,
                'proceeds_eur':  proceeds,
                'pnl_pct':       pnl_pct,
                'confidence':    confidence,
                'reasoning':     sell_reason,
                'full_exit':     not partial,
            })
            sell_reasons[ticker] = sell_reason
            _save_rec(ticker, 'SELL', confidence, sell_reason,
                      'portfolio_brain,sector_rebalance',
                      {'key_risks': f'P&L at time of signal: {pnl_pct:.1f}%'})
        else:
            # Holdings not being sold: still show as HOLD context (not surfaced in UI)
            pass

    # ── 2. Build buy list from underweight sectors + trending overrides ───────
    available = float(cash_eur) + freed_eur
    # Use all available cash as the deployment budget. The monthly_budget figure tracks
    # how much new money has arrived this month (for Telegram reporting), but it should
    # not cap spending of cash that's already sitting in the account.
    budget    = available
    buy_slots = int(_setting('max_stocks_per_day', 5))

    buy_candidates = []   # (score, ticker, candidate_dict, reason)

    # Pre-compute per-sector slot caps from user targets (same logic as reconciliation)
    _sector_slot_cap: dict[str, int] = {}
    if targets:
        for _sec, _tgt in targets.items():
            _eff = min(float(_tgt) + 5, 40)
            _sector_slot_cap[_sec] = max(1, round(10 * _eff / 100))

    # Pre-compute per-region slot caps from user region targets
    _region_slot_cap: dict[str, int] = {}
    for _reg, _tgt in reg_targets.items():
        _eff = min(float(_tgt) + 10, 80)  # 10% tolerance for regions (broader categories)
        _region_slot_cap[_reg] = max(1, round(10 * _eff / 100))

    # Track committed slots per sector and region
    _sector_committed: dict[str, int] = {}
    _region_committed: dict[str, int] = {}

    # Cache country → region for held tickers
    if holdings:
        _held_tickers = [h['ticker'] for h in holdings]
        _crows = query("SELECT ticker, country FROM universe WHERE ticker = ANY(%s)", (_held_tickers,))
        _ticker_country_held = {r['ticker']: r['country'] for r in _crows}
    else:
        _ticker_country_held = {}

    def _sector_has_room(sec: str) -> bool:
        if not _sector_slot_cap:
            return True
        cap = _sector_slot_cap.get(sec, 4)
        already_held = sum(1 for h in holdings
                           if normalize_sector(h.get('sector', '')) == sec)
        return (already_held + _sector_committed.get(sec, 0) + 1) <= cap

    def _region_has_room(region: str) -> bool:
        if not _region_slot_cap:
            return True
        cap = _region_slot_cap.get(region, 8)  # generous default
        already_held = sum(1 for h in holdings
                           if country_to_region((_ticker_country_held.get(h['ticker']) or 'US')) == region)
        return (already_held + _region_committed.get(region, 0) + 1) <= cap

    def _sector_commit(sec: str):
        _sector_committed[sec] = _sector_committed.get(sec, 0) + 1

    def _region_commit(region: str):
        _region_committed[region] = _region_committed.get(region, 0) + 1

    # (a) Underweight sectors — find best candidate per sector
    for sector, tgt_pct in sorted(targets.items(), key=lambda x: -x[1]):
        if len(buy_candidates) >= buy_slots * 2:
            break
        cur_pct    = float(exposure.get(sector, 0))
        under      = float(tgt_pct) - cur_pct
        if under < 2.0:
            continue  # close enough, skip
        candidates = [d for d in disc_by_sector.get(sector, [])
                      if d.get('direction') == 'BUY'
                      and d['ticker'] not in held_set
                      and d['ticker'] not in {s['ticker'] for s in sells}]
        if not candidates:
            continue
        best = candidates[0]  # already sorted by score
        ta   = ta_recs.get(best['ticker'], {})
        ta_conf = float(ta.get('confidence') or 0.55)
        score   = float(best.get('blended_score') or best.get('total_score', 0))
        reason  = (
            f'Sector {sector} underweight by {under:.1f}% vs target {tgt_pct:.0f}%. '
            + (best.get('reason') or best.get('event_type', ''))
        )
        if ta.get('action') == 'BUY':
            reason += f' TradingAgents: BUY ({ta_conf:.0%}).'
            score  += 10  # bonus for TA alignment
        # Also check region room before committing
        _cand_country_row = query("SELECT country FROM universe WHERE ticker=%s LIMIT 1", (best['ticker'],))
        _cand_region = country_to_region((_cand_country_row[0]['country'] if _cand_country_row else 'US') or 'US')
        if not _region_has_room(_cand_region):
            log('INFO', 'portfolio_brain',
                f'{best["ticker"]}: region {_cand_region} at cap — skipping underweight-sector candidate')
            continue
        _sector_commit(sector)
        _region_commit(_cand_region)
        buy_candidates.append((score, best['ticker'], best, reason, ta_conf))

    # (b) TradingAgents BUY injection — TA BUY signals respecting sector slot caps
    for ta_ticker, ta in ta_recs.items():
        if ta.get('action') != 'BUY':
            continue
        ta_conf = float(ta.get('confidence') or 0)
        if ta_conf < 0.6:
            continue
        if ta_ticker in held_set:
            continue
        if ta_ticker in {c[1] for c in buy_candidates}:
            continue
        d = disc_by_ticker.get(ta_ticker)
        # Use discovery score if available; fall back to TA confidence × 60 (so 0.75 → score=45)
        if d and d.get('direction') == 'BUY':
            score = float(d.get('blended_score') or d.get('total_score', 0)) + 15
            event_hint = d.get('reason') or d.get('event_type', '')
        else:
            # No discovery entry — TA found it via watchlist/holdings analysis
            # Use TA confidence as proxy score
            score = ta_conf * 60 + 10  # e.g. 0.80 → 58, 0.75 → 55
            event_hint = ta.get('reasoning', '')[:80] if ta.get('reasoning') else 'TradingAgents signal'
            # Create a minimal pseudo-discovery entry for downstream sizing
            uni = query("SELECT sector, company_name, country, exchange FROM universe WHERE ticker=%s LIMIT 1", (ta_ticker,))
            _exchange = (uni[0]['exchange'] or '') if uni else ''
            _ccy = 'GBP' if _exchange in ('LSE', 'LON') or ta_ticker.endswith('.L') else \
                   'EUR' if _exchange in ('XETRA', 'ETR', 'GER') or ta_ticker.endswith('.DE') else \
                   'USD'
            _raw_sector = (uni[0]['sector'] if uni else '') or ''
            d = {
                'ticker': ta_ticker,
                'direction': 'BUY',
                'sector': normalize_sector(_raw_sector),
                'company_name': uni[0]['company_name'] if uni else ta_ticker,
                'currency': _ccy,
                'total_score': score,
                'tax_notes': '',
                'fx_risk': 'medium',
                'event_type': 'TRADINGAGENTS',
            }
        # Check sector and region caps before adding
        ta_sector = normalize_sector(d.get('sector', '') or '')
        if not _sector_has_room(ta_sector):
            log('INFO', 'portfolio_brain',
                f'{ta_ticker}: sector {ta_sector} at cap — skipping TA injection')
            continue
        _ta_country_row = query("SELECT country FROM universe WHERE ticker=%s LIMIT 1", (ta_ticker,))
        _ta_region = country_to_region((_ta_country_row[0]['country'] if _ta_country_row else 'US') or 'US')
        if not _region_has_room(_ta_region):
            log('INFO', 'portfolio_brain',
                f'{ta_ticker}: region {_ta_region} at cap — skipping TA injection')
            continue
        _sector_commit(ta_sector)
        _region_commit(_ta_region)

        gdelt = _gdelt_signal(ta_ticker)
        poly  = _polymarket_signal(ta_ticker)
        reason = f'TradingAgents BUY ({ta_conf:.0%}): {event_hint}. '
        if gdelt.get('is_accelerating'):
            reason += f'GDELT {float(gdelt.get("mention_velocity",1)):.1f}x. '
        if poly:
            reason += f'Polymarket: {poly["question"][:60]} ({float(poly["probability"]):.0%}). '
        buy_candidates.append((score, ta_ticker, d, reason, ta_conf))

    # (c) Trending overrides — strong signals regardless of sector weight
    override_score = float(trending_override)
    for d in discovery:
        if len(buy_candidates) >= buy_slots * 3:
            break
        if d['ticker'] in held_set:
            continue
        if d['ticker'] in {c[1] for c in buy_candidates}:
            continue
        score = float(d.get('blended_score') or d.get('total_score', 0))
        if score < override_score:
            continue
        ta = ta_recs.get(d['ticker'], {})
        gdelt = _gdelt_signal(d['ticker'])
        poly  = _polymarket_signal(d['ticker'])
        conf  = float(ta.get('confidence') or 0.6)
        reason = (
            f'High-conviction opportunity (score {score:.0f}): '
            + (d.get('reason') or d.get('event_type', '')) + '. '
        )
        if gdelt.get('is_accelerating'):
            reason += f'GDELT accelerating ({float(gdelt.get("mention_velocity",1)):.1f}x). '
        if poly:
            reason += f'Polymarket: {poly["question"][:60]} ({float(poly["probability"]):.0%}). '
        buy_candidates.append((score, d['ticker'], d, reason, conf))

    # Get region of each candidate ticker for geographic diversification bonus
    candidate_tickers = list({c[1] for c in buy_candidates})
    if candidate_tickers:
        country_rows = query("SELECT ticker, country FROM universe WHERE ticker = ANY(%s)",
                             (candidate_tickers,))
        ticker_country = {r['ticker']: r['country'] for r in country_rows}
    else:
        ticker_country = {}

    # Sort by expected value = discovery_score × ta_confidence + region underweight bonus
    def _ev(item):
        score, ticker, cand, reason, conf = item
        ta = ta_recs.get(ticker, {})
        ta_conf = float(ta.get('confidence') or conf or 0.5)
        ev = score * max(0.5, ta_conf)
        # Bonus for regions that are underweight vs target
        country = ticker_country.get(ticker, 'US') or 'US'
        region = country_to_region(country)
        tgt_pct = float(reg_targets.get(region, 0))
        cur_pct = float(region_exposure.get(region, 0))
        under = tgt_pct - cur_pct
        if under > 10:
            ev += 10.0  # strong underweight bonus
        elif under > 5:
            ev += 5.0
        elif cur_pct - tgt_pct > 20:
            ev -= 8.0   # penalty for heavily overweight region
        return ev

    buy_candidates.sort(key=lambda x: -_ev(x))
    seen_buy = set()
    final_buys = []
    for score, ticker, cand, reason, conf in buy_candidates:
        if ticker in seen_buy:
            continue
        seen_buy.add(ticker)
        final_buys.append((score, ticker, cand, reason, conf))

    log('INFO', 'portfolio_brain',
        f'{len(final_buys)} buy candidates before conviction filter '
        f'(budget €{budget:.0f}, target: deploy ≥80%)')

    # ── PyPortfolioOpt: compute optimal weights for all buy candidates ────────
    buy_tickers = [t for _, t, _, _, _ in final_buys]
    opt_weights = optimize_candidates(buy_tickers) if len(buy_tickers) >= 2 else {}
    if opt_weights:
        log('INFO', 'portfolio_brain',
            f'Optimizer weights: {", ".join(f"{t}={w:.1%}" for t,w in opt_weights.items() if w>0)}')

    spent_eur = 0.0
    for score, ticker, cand, reason, conf in final_buys:
        remaining_budget = (budget - spent_eur) * size_mult
        if remaining_budget < MIN_TRADE_VALUE_EUR:
            break

        # Conviction gate: TradingAgents BUY is sufficient on its own.
        # Still collect all sources for logging/context.
        _, conviction_sources = _conviction_check(ticker, ta_recs, disc_by_ticker)
        ta_this = ta_recs.get(ticker, {})
        ta_action = ta_this.get('action', '')
        ta_conf_val = float(ta_this.get('confidence') or 0)
        ta_buy = ta_action == 'BUY' and ta_conf_val >= effective_min_conf

        if not ta_buy and not conviction_sources:
            # No TA BUY and no supporting signals — skip
            watches.append({
                'ticker':      ticker,
                'company_name': cand.get('company_name', ticker),
                'sector':      cand.get('sector', ''),
                'action':      'WATCH',
                'confidence':  conf,
                'reasoning':   reason,
                'trigger':     f'No TA BUY signal — regime: {regime["regime"]}',
            })
            _save_rec(ticker, 'WATCH', conf,
                      reason + f' [no TA BUY for {regime["regime"]} market]',
                      'portfolio_brain,conviction_gate')
            continue

        if not conviction_sources:
            conviction_sources = [f"TradingAgents({ta_conf_val:.0%})"]

        if conf < effective_min_conf:
            watches.append({
                'ticker':    ticker,
                'company_name': cand.get('company_name', ticker),
                'sector':    cand.get('sector', ''),
                'action':    'WATCH',
                'confidence': conf,
                'reasoning': reason,
                'trigger':   _watch_trigger(ticker, conf),
            })
            _save_rec(ticker, 'WATCH', conf, reason, 'portfolio_brain,opportunity')
            continue

        weight_hint = get_weight_hint(ticker, opt_weights)  # None → falls back to equal-weight
        sizing = _size_position(ticker, cand.get('currency', 'USD'),
                                remaining_budget,
                                weight_hint=weight_hint,
                                total_budget=budget)
        if not sizing:
            # Not affordable with remaining budget — WATCH, not a hard failure
            price_est = _price_eur(ticker, cand.get('currency', 'USD')) or 0
            if price_est > 0 and price_est > remaining_budget:
                log('INFO', 'portfolio_brain',
                    f'{ticker}: price €{price_est:.0f} > remaining budget €{remaining_budget:.0f} — skipping')
            watches.append({
                'ticker':    ticker,
                'company_name': cand.get('company_name', ticker),
                'sector':    cand.get('sector', ''),
                'action':    'WATCH',
                'confidence': conf,
                'reasoning': reason,
                'trigger':   f'Budget exhausted — add €{max(0, price_est - remaining_budget):.0f} more',
            })
            _save_rec(ticker, 'WATCH', conf, reason, 'portfolio_brain,insufficient_cash')
            continue

        shares, price_eur, cost = sizing

        # Enforce minimum trade value — broker min fees make tiny trades uneconomical
        if cost < MIN_TRADE_VALUE_EUR:
            min_shares = math.ceil(MIN_TRADE_VALUE_EUR / price_eur)
            min_cost = round(min_shares * price_eur, 2)
            if min_cost <= remaining_budget:
                shares, cost = min_shares, min_cost
                log('INFO', 'portfolio_brain',
                    f'{ticker}: bumped to {shares} shares (€{cost:.0f}) to meet MIN_TRADE_VALUE_EUR={MIN_TRADE_VALUE_EUR:.0f}')
            else:
                log('INFO', 'portfolio_brain',
                    f'{ticker}: €{cost:.0f} below min €{MIN_TRADE_VALUE_EUR:.0f} and cannot round up within budget — WATCH')
                watches.append({
                    'ticker':       ticker,
                    'company_name': cand.get('company_name', ticker),
                    'sector':       cand.get('sector', ''),
                    'action':       'WATCH',
                    'confidence':   conf,
                    'reasoning':    reason,
                    'trigger':      f'Below min trade value €{MIN_TRADE_VALUE_EUR:.0f} — add more capital',
                })
                _save_rec(ticker, 'WATCH', conf, reason, 'portfolio_brain,below_min_trade')
                continue

        if freed_eur >= cost:
            funding = f'proceeds from sells (€{freed_eur:.0f} freed)'
            freed_eur -= cost
        elif float(cash_eur) >= cost:
            funding = f'existing cash (€{cash_eur:.0f})'
        else:
            funding = f'fresh capital required (€{cost:.0f})'

        spent_eur += cost
        opt_note = f' PyPortfolioOpt weight: {weight_hint:.1%}.' if weight_hint else ''
        full_reason = (reason + opt_note +
            f' [{regime["regime"]} market, {len(conviction_sources)} signals: '
            f'{", ".join(conviction_sources)}]')

        log('INFO', 'portfolio_brain',
            f'BUY {ticker}: {shares} shares @ €{price_eur:.2f} = €{cost:.0f} '
            f'(deployed: €{spent_eur:.0f}/{budget:.0f})')

        buys.append({
            'ticker':          ticker,
            'company_name':    cand.get('company_name', ticker),
            'sector':          cand.get('sector', ''),
            'action':          'BUY',
            'shares':          shares,
            'price_eur':       price_eur,
            'total_cost_eur':  cost,
            'confidence':      conf,
            'reasoning':       full_reason,
            'funding_source':  funding,
            'tax_notes':       cand.get('tax_notes', ''),
            'event_type':      cand.get('event_type', ''),
            'score':           score,
            'conviction':      conviction_sources,
        })
        _save_rec(ticker, 'BUY', conf, full_reason,
                  f'portfolio_brain,{",".join(conviction_sources)}',
                  {'bull_case': cand.get('reason', ''), 'time_horizon': 'medium-term'})

    # ── 3. Portfolio value + cash deployment summary ─────────────────────────
    portfolio_value = sum(h['value_eur'] for h in holdings)
    fresh_needed    = max(0, sum(b['total_cost_eur'] for b in buys)
                         - float(cash_eur) - freed_eur)
    utilisation = spent_eur / budget * 100 if budget > 0 else 0
    if utilisation < 70 and budget > 300:
        unanalysed = [d['ticker'] for d in discovery
                      if d['ticker'] not in ta_recs
                      and d.get('direction') == 'BUY'
                      and d['ticker'] not in held_set
                      and d['ticker'] not in {b['ticker'] for b in buys}]
        log('WARNING', 'portfolio_brain',
            f'Cash utilisation {utilisation:.0f}% (€{spent_eur:.0f}/€{budget:.0f}). '
            f'{len(unanalysed)} BUY candidates not yet analyzed by TradingAgents. '
            f'Consider increasing max_stocks_per_day to deploy more capital.')
    else:
        log('INFO', 'portfolio_brain',
            f'Cash deployment: €{spent_eur:.0f}/€{budget:.0f} ({utilisation:.0f}% utilised)')

    brief = {
        'date':                date.today(),
        'portfolio_value_eur': round(portfolio_value, 2),
        'cash_eur':            float(cash_eur),
        'sells':               sells,
        'buys':                buys,
        'watches':             watches[:8],
        'freed_cash_eur':      round(freed_eur, 2),
        'fresh_capital_needed': fresh_needed,
        'sector_exposure':     exposure,
        'sector_targets':      targets,
        'region_exposure':     region_exposure,
        'region_targets':      reg_targets,
        'regime':              regime,
    }

    log('INFO', 'portfolio_brain',
        f'Brief: {len(sells)} sells, {len(buys)} buys, {len(watches)} watches '
        f'| portfolio €{portfolio_value:.0f} cash €{cash_eur:.0f}')

    # Trigger auto-executor if enabled
    try:
        from portfolio.auto_executor import run as auto_run
        auto_run(brief)
    except Exception as e:
        log('WARNING', 'portfolio_brain', f'Auto-executor error: {e}')

    return brief


if __name__ == '__main__':
    brief = build_brief()
    print(f"\n=== PORTFOLIO BRIEF {brief['date']} ===")
    print(f"Value: €{brief['portfolio_value_eur']:.2f}  Cash: €{brief['cash_eur']:.2f}")

    print(f"\n🔴 SELLS ({len(brief['sells'])}):")
    for s in brief['sells']:
        flag = '⚠ STOP-LOSS' if s['pnl_pct'] < -10 else ''
        print(f"  {s['ticker']} ({s['sector']}): sell {s['shares']:.0f} @ €{s['price_eur']:.2f}"
              f" = €{s['proceeds_eur']:.0f}  P&L {s['pnl_pct']:+.1f}%  {flag}")
        print(f"    → {s['reasoning'][:100]}")

    print(f"\n🟢 BUYS ({len(brief['buys'])}):")
    for b in brief['buys']:
        print(f"  {b['ticker']} ({b['sector']}): {b['shares']} shares @ €{b['price_eur']:.2f}"
              f" = €{b['total_cost_eur']:.0f}  via {b['funding_source']}")
        print(f"    → {b['reasoning'][:100]}")

    print(f"\n👁 WATCHES ({len(brief['watches'])}):")
    for w in brief['watches']:
        print(f"  {w['ticker']} ({w['sector']}): {w['trigger']}")

    print(f"\n📊 Sector exposure:")
    targets = brief['sector_targets']
    for s, cur in sorted(brief['sector_exposure'].items(), key=lambda x: -x[1]):
        tgt = targets.get(s, 0)
        gap = cur - tgt
        bar = '▓' * int(cur / 2)
        print(f"  {s:30s} {bar:20s} {cur:5.1f}% (target {tgt:.0f}%, gap {gap:+.1f}%)")

    print(f"\n🌍 Geographic exposure:")
    for region, pct in brief.get('region_exposure', {}).items():
        bar = '▓' * int(pct / 4)
        print(f"  {region:20s} {bar:25s} {pct:5.1f}%")
