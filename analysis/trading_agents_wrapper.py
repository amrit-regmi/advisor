"""
TradingAgents orchestration — 9-agent LLM debate pipeline.

Design rationale
----------------
Groq Llama 3.3 70B handles the 7 preliminary agents (fast, cheap, ~4k tokens/ticker).
OpenRouter Nvidia Nemotron handles the 2 critical decision agents (better reasoning,
exactly 2 OR requests/ticker, which fits the free-tier 50 req/day per key).

API key rotation: all GROQ_* and OPENROUTER_* env-vars are auto-discovered. Adding
GROQ_API_KEY_2 (or _3, etc.) to .env doubles/triples the daily budget automatically.
When one key hits a rate limit (429), the pool silently rotates to the next key.

OR model fallback list: primary model first, then successively weaker fallbacks.
This matters on free tier because popular models occasionally exhaust capacity mid-day.
"""
import os
import sys
import time
import json
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')

sys.path.insert(0, '/home/ubuntu/advisor')

from openai import OpenAI
from db.database import query, execute, log
from analysis.rate_limit_manager import get_manager

GROQ_MODEL = 'llama-3.3-70b-versatile'

# OpenRouter free models — in preference order.
# Add/change slugs here without touching other code.
OR_MODELS = [
    'openai/gpt-oss-120b:free',                     # primary: preferred
    'nvidia/nemotron-3-super-120b-a12b:free',        # secondary
    'meta-llama/llama-3.3-70b-instruct:free',        # tertiary fallback
    'nousresearch/hermes-3-llama-3.1-405b:free',     # last resort
]

_DAILY_TICKER_LIMIT_DEFAULT = int(os.getenv('DAILY_ANALYSIS_COUNT', 20))
_MAX_HOLDINGS_DEFAULT       = int(os.getenv('MAX_HOLDINGS', 10))


def _discover_keys(env_prefix: str) -> list[str]:
    """
    Auto-discover all API keys whose env-var name starts with `env_prefix`.
    Reads from both the .env file and os.environ (runtime overrides win).
    Deduplicates by value so the same key on two names is counted once.
    """
    from dotenv import dotenv_values
    file_env = dotenv_values('/home/ubuntu/advisor/.env')
    combined = {**file_env, **os.environ}   # os.environ wins on conflict
    seen, keys = set(), []
    for name in sorted(combined.keys()):
        if name.startswith(env_prefix):
            val = combined[name].strip() if combined[name] else ''
            if val and val not in seen:
                seen.add(val)
                keys.append(val)
    return keys


class _KeyPool:
    """
    Rotating pool of API keys for one provider.
    On 429 / rate-limit, rotates to the next key automatically.
    Thread-safe enough for single-process sequential use.
    """
    def __init__(self, env_prefix: str, base_url: str):
        self._prefix   = env_prefix
        self._base_url = base_url
        self._refresh()

    def _refresh(self):
        self._keys    = _discover_keys(self._prefix)
        self._idx     = 0
        self._clients = [
            OpenAI(api_key=k, base_url=self._base_url)
            for k in self._keys
        ]
        if self._keys:
            log('INFO', 'trading_agents',
                f'{self._prefix}: {len(self._keys)} API key(s) loaded')

    @property
    def client(self) -> OpenAI:
        if not self._clients:
            raise RuntimeError(f'No API keys found for prefix {self._prefix}')
        return self._clients[self._idx]

    def rotate(self) -> bool:
        """Rotate to next key. Returns True if a new key is available."""
        if len(self._clients) <= 1:
            return False
        self._idx = (self._idx + 1) % len(self._clients)
        log('INFO', 'trading_agents',
            f'{self._prefix}: rotated to key #{self._idx + 1}/{len(self._clients)}')
        return True

    @property
    def key_index(self) -> int:
        return self._idx + 1

    @property
    def key_count(self) -> int:
        return len(self._clients)


_groq_pool = None
_or_pool   = None


def _groq_pool_get() -> _KeyPool:
    global _groq_pool
    if _groq_pool is None:
        _groq_pool = _KeyPool('GROQ', 'https://api.groq.com/openai/v1')
    return _groq_pool


def _or_pool_get() -> _KeyPool:
    global _or_pool
    if _or_pool is None:
        _or_pool = _KeyPool('OPENROUTER', 'https://openrouter.ai/api/v1')
    return _or_pool


def _call_groq(prompt: str, max_tokens: int = 120, retries: int = 3) -> str:
    """Call Groq 70B with key rotation on rate-limit.

    Prompt is hard-capped at 3000 chars. Groq free tier starts returning garbled
    or truncated output above ~3500 chars; 3000 gives a safe margin.
    """
    pool = _groq_pool_get()
    for attempt in range(retries):
        try:
            resp = pool.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{'role': 'user', 'content': prompt[:3000]}],
                max_tokens=max_tokens,
                temperature=0.3,
            )
            return resp.choices[0].message.content or ''
        except Exception as e:
            err = str(e)
            if '429' in err or 'rate_limit' in err.lower():
                if pool.rotate():
                    log('INFO', 'trading_agents',
                        f'Groq rate limit — rotated to key #{pool.key_index}')
                    continue
                wait = 62  # 60s TPM window + 2s buffer; sleep until the window resets
                log('WARNING', 'trading_agents', f'Groq all keys rate-limited — waiting {wait}s')
                time.sleep(wait)
            else:
                raise
    return ''


def _try_or_call(pool: _KeyPool, model: str, system: str, user: str,
                 max_tokens: int) -> tuple[str, str]:
    """
    Attempt one OpenRouter call.
    Returns (result, status) where status is:
      'ok'         — success, result has content
      'rotate'     — 429 rate limit, caller should rotate key and retry
      'skip_model' — 404/503-exhausted/other, skip to next model
    """
    msgs = [
        {'role': 'system', 'content': system[:1500]},
        {'role': 'user',   'content': user[:2500]},
    ]
    try:
        content = pool.client.chat.completions.create(
            model=model, messages=msgs, max_tokens=max_tokens, temperature=0.2,
        ).choices[0].message.content or ''
        return content, 'ok'
    except Exception as e:
        err = str(e)
        if '429' in err or 'rate_limit' in err.lower() or 'per-day' in err.lower():
            return '', 'rotate'
        elif '503' in err or '502' in err:
            # Some free OR models return 503 when a system role is present.
            # Merging system + user into one user message usually unblocks it.
            try:
                content = pool.client.chat.completions.create(
                    model=model,
                    messages=[{'role': 'user',
                               'content': f"{system}\n\n{user}"[:4000]}],
                    max_tokens=max_tokens, temperature=0.2,
                ).choices[0].message.content or ''
                return content, 'ok'
            except Exception:
                pass
            return '', 'skip_model'
        elif 'instruction is not enabled' in err or ('400' in err and 'system' in err.lower()):
            # System messages not supported — retry with merged user message
            try:
                content = pool.client.chat.completions.create(
                    model=model,
                    messages=[{'role': 'user',
                               'content': f"{system}\n\n{user}"[:4000]}],
                    max_tokens=max_tokens, temperature=0.2,
                ).choices[0].message.content or ''
                return content, 'ok'
            except Exception:
                pass
            return '', 'skip_model'
        elif '404' in err or 'No endpoints' in err:
            return '', 'skip_model'
        else:
            log('WARNING', 'trading_agents', f'OR {model}: {err[:80]}')
            return '', 'skip_model'


def _call_openrouter(system: str, user: str, max_tokens: int = 250,
                     pool: str = 'daily20') -> str:
    """
    Call OpenRouter with key rotation and model fallback.
    For each model: try each key once. 429 → rotate key. Other errors → next model.
    Records a successful call against `pool` in the rate limit manager.
    Returns empty string when all key×model combos exhausted.
    """
    pool_obj = _or_pool_get()

    for model in OR_MODELS:
        tried: set[int] = set()

        while len(tried) < pool_obj.key_count:
            tried.add(pool_obj.key_index)
            result, status = _try_or_call(pool_obj, model, system, user, max_tokens)

            if status == 'ok':
                get_manager().record_or_call(pool)
                return result
            elif status == 'rotate':
                log('WARNING', 'trading_agents',
                    f'OR key #{pool_obj.key_index} rate-limited on {model}')
                if not pool_obj.rotate() or pool_obj.key_index in tried:
                    break  # all keys exhausted for this model
            else:  # skip_model
                break  # move to next model

    log('ERROR', 'trading_agents', 'All OpenRouter keys+models exhausted')
    return ''


# ── Data helpers ─────────────────────────────────────────────────────────────

def _gdelt(ticker: str) -> str:
    # Fetch last 3 days so we can compute a rolling FinBERT average
    rows = query("""
        SELECT article_count, avg_tone, mention_velocity, is_accelerating,
               finbert_sentiment_label, finbert_confidence, finbert_sentiment_score
        FROM news_sentiment WHERE ticker=%s AND date>=%s ORDER BY date DESC LIMIT 3
    """, (ticker, date.today() - timedelta(days=7)))
    if not rows:
        return 'no news'
    r = rows[0]
    acc = '+ACCEL' if r.get('is_accelerating') else ''

    fb_label = (r.get('finbert_sentiment_label') or '').strip()
    fb_conf  = float(r.get('finbert_confidence') or 0)
    fb_str   = f" fb={fb_label}({fb_conf:.0%})" if fb_label else ''

    # 3-day rolling mean of the FinBERT score (range -1 to +1)
    fb_scores = [float(row['finbert_sentiment_score'])
                 for row in rows if row.get('finbert_sentiment_score') is not None]
    fb_3d = f" fb3d={sum(fb_scores)/len(fb_scores):+.2f}" if fb_scores else ''

    return (f"{r['article_count']}art tone={float(r['avg_tone'] or 0):+.1f} "
            f"vel={float(r['mention_velocity'] or 1):.1f}x{acc}{fb_str}{fb_3d}")


def _price(ticker: str) -> str:
    rows = query("""
        SELECT close FROM prices WHERE ticker=%s AND date>=%s ORDER BY date DESC LIMIT 7
    """, (ticker, date.today() - timedelta(days=10)))
    prices = [float(r['close']) for r in rows if r.get('close')]
    if not prices:
        return 'no price'
    pct = (prices[0] - prices[-1]) / prices[-1] * 100 if len(prices) > 1 else 0
    return f"{prices[0]:.2f}({pct:+.1f}%7d)"


def _macro() -> str:
    rows = query("""
        SELECT series_id, value FROM macro_data WHERE date>=%s ORDER BY date DESC LIMIT 3
    """, (date.today() - timedelta(days=30),))
    if not rows:
        return 'stable'
    return ' '.join(f"{r['series_id']}={float(r['value'] or 0):.1f}" for r in rows)


def _get_setting(key: str, default=0):
    row = query("SELECT value FROM user_settings WHERE key=%s LIMIT 1", (key,))
    if not row:
        return default
    try:
        return float(row[0]['value'])
    except (TypeError, ValueError):
        return row[0]['value']  # return raw string for non-numeric settings


def _portfolio() -> dict:
    h = query("SELECT ticker FROM holdings WHERE active=true AND shares>0")
    cash = _get_setting('nordnet_cash_eur', 0)
    sim_val = _get_setting('simulate_recommendations', 'false')
    simulate_on = str(sim_val).lower() == 'true'
    if simulate_on:
        monthly_budget = _get_setting('simulation_monthly_deposit',
                                      _get_setting('monthly_investment_budget_eur', 500))
    else:
        monthly_budget = _get_setting('monthly_investment_budget_eur', 500)
    monthly_invested = _get_setting('monthly_invested_this_month', 0)
    budget_left = max(0, float(monthly_budget) - float(monthly_invested))
    return {
        'n':           len(h),
        'tickers':     [r['ticker'] for r in h],
        'cash':        float(cash),
        'budget_left': budget_left,
    }


# One-line legend prepended to every agent prompt so models interpret
# our custom signal scales correctly without any assumed prior knowledge.
_SIGNAL_KEY = (
    "Scales: tone[-10..+10] vel[1x=avg,>1=elevated] conv[0..1,≥0.70=strong] "
    "fb_conf[0..1=FinBERT certainty] fb3d[-1..+1=3d FinBERT avg]"
)


def _data(ticker: str, state: str, conviction: float, port: dict) -> str:
    """Compact data block passed into every agent prompt."""
    return (
        f"{_SIGNAL_KEY}\n"
        f"{ticker}|{state}|conv={conviction:.2f}|"
        f"P={_price(ticker)}|N={_gdelt(ticker)}|"
        f"M={_macro()}|"
        f"port={port['n']}/{MAX_HOLDINGS}|cash={port['cash']:.0f}EUR"
        f"|budget={port['budget_left']:.0f}EUR"
    )


# ── Agent functions ───────────────────────────────────────────────────────────

def _analyst_market(d: str) -> str:
    return _call_groq(
        f"Market analyst: assess price trend, momentum, and technicals. "
        f"Give specific numbers and a clear directional view. 80 words.\nData: {d}",
        max_tokens=130,
    )


def _analyst_news(d: str) -> str:
    return _call_groq(
        f"News analyst: assess sentiment velocity, tone, and catalysts. "
        f"Flag any divergence between price direction and sentiment. "
        f"Interpret FinBERT label, confidence, and 3-day average. 80 words.\nData: {d}",
        max_tokens=130,
    )


def _debate_four_rounds(d: str, mkt: str, news: str) -> tuple[str, str]:
    """4-round Bull vs Bear on Groq. Returns (bull_summary, bear_summary)."""
    ctx = f"{d} MKT:{mkt[:120]} NEWS:{news[:120]}"
    bear1 = _call_groq(f"Bear: 3 specific risks with evidence. 70 words.\n{ctx}", max_tokens=110)
    time.sleep(1)   # pace calls to stay within Groq's 28 RPM limit
    bull1 = _call_groq(f"Bull: counter each bear risk with data. 70 words.\n{ctx}\nBear:{bear1[:110]}", max_tokens=110)
    time.sleep(1)
    bear2 = _call_groq(f"Bear: rebut bull, sharpen thesis. 70 words.\n{ctx}\nBull:{bull1[:110]}", max_tokens=110)
    time.sleep(1)
    bull2 = _call_groq(f"Bull: closing argument with conviction. 70 words.\n{ctx}\nBear2:{bear2[:110]}", max_tokens=110)
    return (f"R1:{bull1[:140]}|R2:{bull2[:140]}", f"R1:{bear1[:140]}|R2:{bear2[:140]}")


def _evaluator_groq(d: str, bull: str, bear: str) -> str:
    """Groq synthesizes the 4-round debate into a research verdict."""
    return _call_groq(
        f"Research Evaluator. Synthesize the 4-round bull/bear debate. "
        f"State the strongest argument on each side, then give Verdict: Bull/Bear/Neutral "
        f"and Confidence: 0.0-1.0. 100 words.\n"
        f"Data:{d}\nBull:{bull}\nBear:{bear}",
        max_tokens=160,
    )


def _trader(d: str, research: str) -> str:
    return _call_groq(
        f"Trader: give a preliminary signal BUY/SELL/HOLD/WATCH with a specific rationale. "
        f"Reference the evaluator's verdict and key data points. 60 words.\n"
        f"Data:{d}\nResearch:{research[:200]}",
        max_tokens=95,
    )


# ── 3-way Groq risk debate ────────────────────────────────────────────────────

def _risk_aggressive(d: str, trader_view: str) -> str:
    """Champion the upside — data-driven case for taking the trade."""
    return _call_groq(
        f"Aggressive Risk Analyst: champion the upside of this trade. "
        f"Counter any obvious conservative concerns with data. 50 words.\n"
        f"Trader: {trader_view[:80]}\nData:{d}",
        max_tokens=80,
    )


def _risk_conservative(d: str, trader_view: str) -> str:
    """Focus on capital protection — downside, concentration, rejection case."""
    return _call_groq(
        f"Conservative Risk Analyst: protect capital. Identify the top downside risks, "
        f"concentration issues, and reasons to reduce or reject this trade. 50 words.\n"
        f"Trader: {trader_view[:80]}\nData:{d}",
        max_tokens=80,
    )


def _risk_neutral(d: str, trader_view: str) -> str:
    """Balance aggressive and conservative — objective risk/reward assessment."""
    return _call_groq(
        f"Neutral Risk Analyst: balance upside and downside objectively. "
        f"Weigh the aggressive and conservative perspectives. 50 words.\n"
        f"Trader: {trader_view[:80]}\nData:{d}",
        max_tokens=80,
    )


def _risk_manager_groq(d: str, trader_view: str, risk_debate: str, port: dict) -> str:
    """Groq fallback: synthesise risk debate and challenge the PM."""
    return _call_groq(
        f"Risk Manager. Synthesise the 3-analyst risk debate in 40 words, "
        f"state Verdict: APPROVE/CAUTION/REJECT, max_size=X%, "
        f"then list 2 specific challenges the PM must address.\n"
        f"Data:{d[:150]}\nTrader:{trader_view[:80]}\nDebate:{risk_debate[:200]}",
        max_tokens=150,
    )


def _risk_manager_or(d: str, trader_view: str, risk_debate: str,
                     port: dict, pool: str = 'daily20') -> str:
    """
    OpenRouter call 1 — Risk Manager.
    Synthesises the 3-way Groq risk debate and issues specific challenges
    that the Portfolio Manager must address before making a final decision.
    """
    system = (
        "You are a Risk Manager synthesising a 3-way risk debate (Aggressive, Conservative, Neutral) "
        "and challenging the Portfolio Manager. "
        f"({_SIGNAL_KEY})"
    )
    user = (
        f"Ticker data: {d}\n"
        f"Trader proposes: {trader_view[:120]}\n"
        f"Portfolio: {port['n']}/{MAX_HOLDINGS} positions, "
        f"cash={port['cash']:.0f}EUR, deployable={port['budget_left']:.0f}EUR\n\n"
        f"Risk debate:\n{risk_debate}\n\n"
        f"Instructions:\n"
        f"1. Synthesise the debate in 50 words.\n"
        f"2. State: Verdict: APPROVE / CAUTION / REJECT, max_size=X%\n"
        f"3. Write exactly 2 pointed challenges the PM must address, numbered 1. and 2.\n"
        f"150 words total max."
    )
    result = _call_openrouter(system, user, max_tokens=250, pool=pool)
    if not result:
        log('INFO', 'trading_agents', 'RiskMgr: OR unavailable — using Groq fallback')
        result = _risk_manager_groq(d, trader_view, risk_debate, port)
    return result


def _pm_response_or(d: str, trader_view: str, risk_challenge: str,
                    port: dict, pool: str = 'daily20') -> str:
    """
    OpenRouter call 2 — PM counter-argument to the Risk Manager.
    Uses the same strong model so the debate is symmetric. Not the final decision;
    the PM Final (OR call 4) arbitrates the full debate and produces the JSON verdict.
    Falls back to Groq if OR is exhausted.
    """
    system = (
        "You are a Portfolio Manager responding to a Risk Manager's specific challenges. "
        "Be direct: state your proposed action, then address each numbered challenge with evidence. "
        f"({_SIGNAL_KEY})"
    )
    user = (
        f"Data: {d}\n"
        f"Trader signal: {trader_view[:120]}\n"
        f"Risk Manager verdict + challenges:\n{risk_challenge}\n\n"
        f"Portfolio: {port['n']}/{MAX_HOLDINGS} positions, "
        f"cash={port['cash']:.0f}EUR, deployable={port['budget_left']:.0f}EUR\n\n"
        f"State your proposed action (BUY/SELL/HOLD/WATCH/AVOID) and address each "
        f"numbered challenge. 80 words max."
    )
    result = _call_openrouter(system, user, max_tokens=160, pool=pool)
    if not result:
        # Groq fallback keeps the debate alive even when OR is exhausted
        result = _call_groq(
            f"PM counter-argument to Risk Manager. Propose action + address challenges. 60 words.\n"
            f"Data:{d[:150]}\nTrader:{trader_view[:80]}\nRisk:{risk_challenge[:200]}",
            max_tokens=100,
        )
    return result


def _extract_confidence_from_text(text: str) -> float:
    """Pull a 0-1 confidence float mentioned anywhere in evaluator/research text."""
    import re
    patterns = [
        r'confidence[:\s=]+([0-9]\.[0-9]+)',
        r'confidence[:\s=]+([0-9]+)%',
        r'\b(0\.[5-9][0-9])\b',
        r'\b(0\.[0-4][0-9])\b',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            return val / 100 if val > 1 else val
    return 0.0


def _risk_manager_rebuttal_or(d: str, risk_challenge: str, pm_response: str,
                              pool: str = 'daily20') -> str:
    """
    OpenRouter call 3 — Risk Manager's final position after reading the PM's counter.
    Concedes valid PM points, holds firm on genuine risks, states final verdict.
    Falls back to Groq if OR is exhausted.
    """
    system = (
        "You are a Risk Manager delivering your final position after the Portfolio Manager "
        "has countered your challenges. Concede where the PM's argument is sound; "
        "hold firm where the risk is real. State a final Verdict: APPROVE / CAUTION / REJECT. "
        f"({_SIGNAL_KEY})"
    )
    user = (
        f"Your initial challenges:\n{risk_challenge}\n\n"
        f"PM's counter-argument:\n{pm_response}\n\n"
        f"Deliver your final risk position in 80 words. "
        f"Acknowledge what the PM addressed well, then state your final "
        f"Verdict: APPROVE / CAUTION / REJECT and any remaining conditions."
    )
    result = _call_openrouter(system, user, max_tokens=180, pool=pool)
    if not result:
        result = _call_groq(
            f"Risk Manager final position after PM rebuttal. 60 words. "
            f"Concede valid PM points, hold firm on real risks, state Verdict.\n"
            f"Initial:{risk_challenge[:150]}\nPM counter:{pm_response[:150]}",
            max_tokens=100,
        )
    return result


def _pm_final_or(d: str, trader_view: str, risk_challenge: str, port: dict,
                 research: str = '', pm_draft: str = '', rm_rebuttal: str = '',
                 pool: str = 'daily20') -> dict:
    """
    OpenRouter call 4 — Portfolio Manager final decision.
    Receives the full 4-turn debate (RM challenges → PM response → RM rebuttal)
    and produces the final JSON verdict.
    """
    eval_conf     = _extract_confidence_from_text(research) if research else 0.0
    baseline_conf = eval_conf if eval_conf >= 0.4 else 0.55

    held_tickers = [r['ticker'] for r in query(
        "SELECT ticker FROM holdings WHERE active=TRUE AND shares>0")]
    is_held = d.split('|')[0] in held_tickers

    system = (
        "You are a Portfolio Manager making the final investment decision after a full debate. "
        "You MUST respond with valid JSON only — no explanation, no markdown, no preamble. "
        "Required keys: action, sizing_intent, watchlist_action, rotation_flag, strong_override, "
        f"risk_notes, confidence, reasoning. ({_SIGNAL_KEY})"
    )
    user = (
        f"Make the final investment decision. You have seen the full debate.\n\n"
        f"Data: {d}\n"
        f"Trader signal: {trader_view[:120]}\n"
        f"Risk Manager verdict + challenges: {risk_challenge[:200]}\n"
        f"Your response to challenges: {pm_draft[:120]}\n"
        f"Risk Manager rebuttal: {rm_rebuttal[:150]}\n"
        f"Portfolio: {port['n']}/{MAX_HOLDINGS} positions, "
        f"cash={port['cash']:.0f}EUR, deployable={port['budget_left']:.0f}EUR\n"
        f"Currently held: {'YES' if is_held else 'NO'}\n"
        f"Evaluator baseline confidence (bullish): {baseline_conf:.2f}\n\n"
        f"Rules — apply in order, stop at first match:\n"
        f"1. If trader signal contains SELL or AVOID:\n"
        f"   - SELL if currently held=YES\n"
        f"   - AVOID if currently held=NO\n"
        f"2. If trader=WATCH or confidence<0.55: WATCH\n"
        f"3. If deployable<50: WATCH\n"
        f"4. STRONG_BUY if trader=BUY AND confidence>=0.75 AND risk=APPROVE\n"
        f"5. BUY if trader=BUY AND confidence>=0.60 AND risk=APPROVE\n"
        f"6. WATCH if trader=BUY AND confidence>=0.60 AND risk=CAUTION\n"
        f"7. HOLD if currently held=YES\n"
        f"8. AVOID otherwise\n\n"
        f"In 'reasoning', state your action and explicitly address the Risk Manager's challenges.\n"
        f"Respond with this JSON and nothing else:\n"
        f'{{"action":"BUY","sizing_intent":"increase","watchlist_action":"none",'
        f'"rotation_flag":false,"strong_override":false,'
        f'"risk_notes":"brief risk note","confidence":{baseline_conf:.2f},'
        f'"reasoning":"action because X; addresses risk challenges: Y"}}'
    )
    raw = _call_openrouter(system, user, max_tokens=300, pool=pool)

    if not raw:
        log('INFO', 'trading_agents', 'PM: OR unavailable — using Groq fallback')
        return _pm_final_groq(d, trader_view, risk_challenge, port, research)

    return _parse_pm_response(raw, risk_challenge, baseline_conf)


def _pm_final_groq(d: str, trader_view: str, risk_challenge: str, port: dict,
                   research: str = '') -> dict:
    """Groq fallback for PM when all OR models are exhausted."""
    eval_conf = _extract_confidence_from_text(research) if research else 0.0
    baseline_conf = eval_conf if eval_conf >= 0.4 else 0.55
    held = [r['ticker'] for r in query("SELECT ticker FROM holdings WHERE active=TRUE AND shares>0")]
    is_held = d.split('|')[0] in held

    raw = _call_groq(
        f"Portfolio Manager. Output ONLY valid JSON, no text before or after.\n"
        f"Data:{d[:200]}\nTrader:{trader_view[:80]}\nRisk:{risk_challenge[:120]}\n"
        f"Portfolio:{port['n']}/{MAX_HOLDINGS}, deployable={port['budget_left']:.0f}EUR, held={'YES' if is_held else 'NO'}\n"
        f"Rules (in order): "
        f"1.If trader=SELL→SELL(held=YES) or AVOID(held=NO). "
        f"2.If trader=WATCH or conf<0.55→WATCH. "
        f"3.If deployable<50→WATCH. "
        f"4.BUY if trader=BUY and conf>=0.60 and risk=APPROVE. "
        f"5.WATCH if trader=BUY and risk=CAUTION. "
        f"6.HOLD if held=YES else AVOID.\n"
        f'JSON:{{"action":"WATCH","sizing_intent":"maintain","watchlist_action":"add",'
        f'"rotation_flag":false,"strong_override":false,'
        f'"risk_notes":"brief","confidence":{baseline_conf:.2f},"reasoning":"brief"}}',
        max_tokens=200,
    )
    return _parse_pm_response(raw, risk_challenge, baseline_conf)


def _parse_pm_response(raw: str, risk_view: str, baseline_conf: float) -> dict:
    """Parse PM JSON from raw model output (OR or Groq)."""
    for attempt_raw in [raw, raw.replace('```json', '').replace('```', '')]:
        try:
            s, e = attempt_raw.find('{'), attempt_raw.rfind('}') + 1
            if s >= 0 and e > s:
                p = json.loads(attempt_raw[s:e])
                p['action'] = p.get('action', 'WATCH').upper().replace(' ', '_')
                p.setdefault('sizing_intent', 'maintain')
                p.setdefault('watchlist_action', 'none')
                p.setdefault('rotation_flag', False)
                conf = float(p.get('confidence', baseline_conf))
                p['confidence'] = conf
                p.setdefault('strong_override',
                             p.get('action') == 'STRONG_BUY' and conf >= 0.70)
                p.setdefault('risk_notes', risk_view[:80])
                p.setdefault('reasoning', raw[:100])
                return p
        except Exception:
            continue

    # Text fallback
    action = 'WATCH'
    for w in ['STRONG_BUY', 'BUY', 'SELL', 'HOLD', 'AVOID', 'WATCH']:
        if w in raw.upper():
            action = w
            break
    extracted = _extract_confidence_from_text(raw)
    conf = extracted if extracted >= 0.4 else baseline_conf
    log('WARNING', 'trading_agents', f'PM JSON parse failed — text fallback: {action} conf={conf:.2f}')
    return {
        'action': action, 'sizing_intent': 'maintain', 'watchlist_action': 'add',
        'rotation_flag': False, 'strong_override': False,
        'risk_notes': risk_view[:80], 'confidence': conf,
        'reasoning': raw[:150] if raw else 'OR and Groq both unavailable',
    }


# ── Full pipeline ─────────────────────────────────────────────────────────────

def _run_pipeline(ticker: str, state: str, signals: dict, pool: str = 'daily20') -> dict:
    """
    Full 13-agent pipeline for one ticker.

    Groq ×11 (cheap, fast — preliminary and debate work):
      Market Analyst, News Analyst
      Bull/Bear 4-round investment debate
      Research Evaluator, Trader
      Aggressive Risk, Conservative Risk, Neutral Risk (3-way risk debate)

    OpenRouter ×4 (symmetric debate on the strong model — critical decisions):
      Call 1: Risk Manager   — synthesises 3-way risk debate + challenges PM
      Call 2: PM Response    — PM counters the Risk Manager's challenges
      Call 3: RM Rebuttal    — Risk Manager's final position after PM counter
      Call 4: PM Final       — PM arbitrates full debate → final JSON decision
    """
    rate_mgr = get_manager()
    port     = _portfolio()
    conv     = signals.get('conviction', 0.5)
    d        = _data(ticker, state, conv, port)

    log('INFO', 'trading_agents', f'{ticker}: pipeline start (conv={conv:.2f}, pool={pool})')

    # ── Groq: investment analysis pipeline ─────────────────────────────────

    rate_mgr.check_groq_tokens(200)
    mkt = _analyst_market(d)
    time.sleep(1)

    rate_mgr.check_groq_tokens(200)
    news = _analyst_news(d)
    time.sleep(1)

    for _ in range(4):
        rate_mgr.check_groq_tokens(150)
    bull, bear = _debate_four_rounds(d, mkt, news)
    time.sleep(1)

    rate_mgr.check_groq_tokens(200)
    research = _evaluator_groq(d, bull, bear)
    log('INFO', 'trading_agents', f'{ticker}: evaluator: {research[:80]}')
    time.sleep(1)

    rate_mgr.check_groq_tokens(150)
    trader_view = _trader(d, research)
    log('INFO', 'trading_agents', f'{ticker}: trader: {trader_view[:80]}')
    time.sleep(1)

    # ── Groq: 3-way risk debate ─────────────────────────────────────────────

    rate_mgr.check_groq_tokens(150)
    risk_agg  = _risk_aggressive(d, trader_view)
    time.sleep(1)

    rate_mgr.check_groq_tokens(150)
    risk_con  = _risk_conservative(d, trader_view)
    time.sleep(1)

    rate_mgr.check_groq_tokens(150)
    risk_neu  = _risk_neutral(d, trader_view)
    time.sleep(1)

    risk_debate = (
        f"Aggressive: {risk_agg}\n"
        f"Conservative: {risk_con}\n"
        f"Neutral: {risk_neu}"
    )

    # ── OpenRouter call 1: Risk Manager synthesises + challenges PM ─────────

    rate_mgr.check_openrouter_requests(pool)
    risk_challenge = _risk_manager_or(d, trader_view, risk_debate, port, pool=pool)
    log('INFO', 'trading_agents', f'{ticker}: risk-manager (OR): {risk_challenge[:100]}')
    time.sleep(1)

    # ── OpenRouter call 2: PM Response (same model — symmetric debate) ──────

    rate_mgr.check_openrouter_requests(pool)
    pm_response = _pm_response_or(d, trader_view, risk_challenge, port, pool=pool)
    log('INFO', 'trading_agents', f'{ticker}: pm-response (OR): {pm_response[:80]}')
    time.sleep(1)

    # ── OpenRouter call 3: RM Rebuttal — final position after PM's counter ───

    rate_mgr.check_openrouter_requests(pool)
    rm_rebuttal = _risk_manager_rebuttal_or(d, risk_challenge, pm_response, pool=pool)
    log('INFO', 'trading_agents', f'{ticker}: rm-rebuttal (OR): {rm_rebuttal[:80]}')
    time.sleep(1)

    # ── OpenRouter call 4: PM Final arbitrates full debate → JSON verdict ────

    rate_mgr.check_openrouter_requests(pool)
    decision = _pm_final_or(
        d, trader_view, risk_challenge, port,
        research=research, pm_draft=pm_response, rm_rebuttal=rm_rebuttal, pool=pool,
    )

    rate_mgr.record_ticker_done(pool)

    # Attach full debate trace so it gets persisted to DB
    decision['bull_case']    = bull
    decision['bear_case']    = bear
    decision['key_risks']    = risk_challenge
    decision['debate_trace'] = json.dumps({
        'market_analyst':   mkt,
        'news_analyst':     news,
        'investment_debate': [
            {'round': 1,
             'bear': bear.split('|')[0].replace('R1:', '').strip(),
             'bull': bull.split('|')[0].replace('R1:', '').strip()},
            {'round': 2,
             'bear': (bear.split('|')[1].replace('R2:', '').strip()
                      if '|' in bear else ''),
             'bull': (bull.split('|')[1].replace('R2:', '').strip()
                      if '|' in bull else '')},
        ],
        'evaluator':     research,
        'trader':        trader_view,
        'risk_debate': {
            'aggressive':  risk_agg,
            'conservative':risk_con,
            'neutral':     risk_neu,
        },
        'risk_manager':          risk_challenge,
        'pm_response':           pm_response,
        'risk_manager_rebuttal': rm_rebuttal,
        'pm_reasoning':          decision.get('reasoning', ''),
        'pm_risk_notes': decision.get('risk_notes', ''),
        'pm_action':     decision.get('action', ''),
        'pm_confidence': decision.get('confidence', 0.5),
    })
    return decision


def analyze_ticker(ticker: str, state: str, signals: dict, pool: str = 'daily20') -> dict:
    rate_mgr = get_manager()
    if not rate_mgr.can_run_ticker(pool):
        status = rate_mgr.status()
        if pool == 'ondemand':
            log('WARNING', 'trading_agents', f'{ticker}: on-demand budget exhausted — {status}')
            return _mock_decision(ticker, 'On-demand daily quota reached')
        # The daily pipeline uses 32 OR calls (16 tickers × 2) against an 89-call budget.
        # Exhaustion here means duplicate pipeline runs or a misconfigured key count.
        # Raise so the failure is visible rather than silently producing mock decisions.
        raise RuntimeError(
            f'Daily OR budget exhausted for {ticker}. Used: {status["or_daily20_used"]}/'
            f'{status["or_daily20_used"] + status["or_daily20_remaining"]}. '
            f'Reset at midnight UTC. This should not happen — check for duplicate pipeline runs.'
        )
    log('INFO', 'trading_agents',
        f'Analyzing {ticker} (state={state}, conv={signals.get("conviction", 0.5):.2f}, pool={pool})')
    try:
        result = _run_pipeline(ticker, state, signals, pool)
        log('INFO', 'trading_agents', f'{ticker}: {result["action"]} conf={result["confidence"]:.2f}')
        return result
    except RuntimeError:
        raise
    except Exception as e:
        log('ERROR', 'trading_agents', f'{ticker}: pipeline failed: {e}')
        return _mock_decision(ticker, str(e)[:120])


def _mock_decision(ticker: str, reason: str) -> dict:
    return {
        'action': 'WATCH', 'sizing_intent': 'maintain', 'watchlist_action': 'add',
        'rotation_flag': False, 'strong_override': False,
        'risk_notes': reason, 'confidence': 0.5, 'reasoning': f'Mock: {reason}',
    }


_TA_PROGRESS_FILE = '/tmp/advisor_ta_progress.json'


def _ta_progress_write(total: int, done: int, current: str | None):
    import json as _j
    try:
        Path(_TA_PROGRESS_FILE).write_text(_j.dumps({
            'total': total, 'done': done, 'current': current, 'ts': time.time()
        }))
    except Exception:
        pass


def analyze_daily_set(daily_set: list, signals: dict) -> dict:
    """
    Run pipeline on the daily ticker set (max 20) using the daily20 OR pool.
    daily_set: [{ticker, state}, ...]
    signals:   {ticker: {conviction: float}}
    Returns:   {ticker: pm_final_decision}
    """
    from db.database import get_setting
    DAILY_TICKER_LIMIT = get_setting('daily_analysis_count', _DAILY_TICKER_LIMIT_DEFAULT)
    MAX_HOLDINGS       = get_setting('max_holdings', _MAX_HOLDINGS_DEFAULT)
    results  = {}
    rate_mgr = get_manager()
    cap      = min(len(daily_set), DAILY_TICKER_LIMIT)
    log('INFO', 'trading_agents', f'Daily analysis: {cap} tickers')
    log('INFO', 'trading_agents', f'Budget: {rate_mgr.status()}')
    _ta_progress_write(cap, 0, None)

    for i, item in enumerate(daily_set[:cap]):
        ticker = item['ticker']
        state  = item.get('state', 'discovery')
        sigs   = signals.get(ticker, {'conviction': 0.5})
        _ta_progress_write(cap, i, ticker)
        results[ticker] = analyze_ticker(ticker, state, sigs, pool='daily20')
        _ta_progress_write(cap, i + 1, None)
        time.sleep(2)

    _ta_progress_write(cap, cap, None)
    log('INFO', 'trading_agents', f'Done: {len(results)} analysed. Budget: {rate_mgr.status()}')
    return results


def analyze_ondemand(tickers: list, signals: dict) -> dict:
    """
    On-demand analysis triggered by user (e.g. dashboard button).
    Uses the reserved 'ondemand' OR pool (5 req/day = up to 2 tickers).
    tickers: list of str  OR  [{ticker, state}, ...]
    signals: {ticker: {conviction: float}}
    Returns: {ticker: pm_final_decision}
    """
    from analysis.rate_limit_manager import ON_DEMAND_TICKER_LIMIT
    results  = {}
    rate_mgr = get_manager()

    # Normalise tickers to [{ticker, state}]
    items = []
    for t in tickers:
        if isinstance(t, str):
            items.append({'ticker': t, 'state': 'ondemand'})
        else:
            items.append(t)

    log('INFO', 'trading_agents', f'On-demand analysis: {len(items)} tickers requested')
    log('INFO', 'trading_agents', f'Budget: {rate_mgr.status()}')

    for item in items[:ON_DEMAND_TICKER_LIMIT]:
        ticker = item['ticker']
        state  = item.get('state', 'ondemand')
        sigs   = signals.get(ticker, {'conviction': 0.5})
        if not rate_mgr.can_run_ticker('ondemand'):
            log('WARNING', 'trading_agents', f'{ticker}: on-demand budget exhausted')
            results[ticker] = _mock_decision(ticker, 'On-demand daily quota reached')
            continue
        results[ticker] = analyze_ticker(ticker, state, sigs, pool='ondemand')
        time.sleep(2)

    log('INFO', 'trading_agents', f'On-demand done: {len(results)}. Budget: {rate_mgr.status()}')
    return results


# Backward-compat aliases used by run_daily.sh and older pipeline scripts
analyze_daily_sixteen = analyze_daily_set
analyze_daily_twenty  = analyze_daily_set


def analyze_holdings() -> list:
    """
    Run TradingAgents analysis on all active holdings.
    Used by holdings_monitor.py to check existing positions for SELL signals.
    Returns list of dicts: [{ticker, action, confidence, reasoning, ...}]
    """
    from pipeline.signal_engine import compute_signals_batch

    held = query("""
        SELECT ticker FROM holdings WHERE active = TRUE AND shares > 0
    """)
    if not held:
        log('INFO', 'trading_agents', 'No active holdings to analyze')
        return []

    tickers = [h['ticker'] for h in held]
    log('INFO', 'trading_agents', f'Analyzing {len(tickers)} holdings: {", ".join(tickers)}')

    signals_raw = compute_signals_batch(tickers)
    signals = {t: {'conviction': float(v)} for t, v in signals_raw.items()}

    daily_set = [{'ticker': t, 'state': 'holding'} for t in tickers]
    decisions = analyze_daily_set(daily_set, signals)

    results = []
    for ticker, d in decisions.items():
        results.append({
            'ticker':     ticker,
            'action':     d.get('action', 'HOLD'),
            'confidence': float(d.get('confidence', 0.5)),
            'reasoning':  d.get('reasoning', ''),
            'bull_case':  d.get('bull_case', ''),
            'bear_case':  d.get('bear_case', ''),
            'key_risks':  d.get('key_risks', ''),
        })

    save_recommendations(decisions)
    return results


def save_recommendations(decisions: dict):
    from datetime import date as _date
    today = str(_date.today())
    for ticker, d in decisions.items():
        try:
            # Replace any existing trading_agents rec for this ticker today
            execute(
                "DELETE FROM recommendations WHERE ticker=%s AND date=%s AND signal_sources='trading_agents'",
                (ticker, today)
            )
            execute("""
                INSERT INTO recommendations
                    (ticker, action, confidence, reasoning, date, signal_sources,
                     bull_case, bear_case, key_risks, debate_trace, created_at)
                VALUES (%s, %s, %s, %s, %s, 'trading_agents',
                        %s, %s, %s, %s, NOW())
            """, (
                ticker, d['action'], d['confidence'],
                (d.get('reasoning') or '')[:2000],
                today,
                (d.get('bull_case') or '')[:2000],
                (d.get('bear_case') or '')[:2000],
                (d.get('key_risks') or '')[:2000],
                (d.get('debate_trace') or '')[:8000],
            ))
        except Exception as e:
            log('WARNING', 'trading_agents', f'Save {ticker}: {e}')


if __name__ == '__main__':
    from pipeline.daily_selection import build_daily_selection
    from pipeline.signal_engine import compute_signals_batch
    from pipeline.price_collector import fetch_and_store_prices

    daily_set = build_daily_selection()
    if not daily_set:
        print('No tickers from daily_selection — nothing to analyze')
    else:
        tickers = [item['ticker'] for item in daily_set]

        # Ensure every Daily-16 ticker has price data before analysis starts.
        # collect_all.py only fetches prices for watchlist + discovery candidates;
        # rotation-fill tickers may not have prices yet.
        missing = [t for t in tickers
                   if not query("SELECT 1 FROM prices WHERE ticker=%s LIMIT 1", (t,))]
        if missing:
            log('INFO', 'trading_agents',
                f'Pre-fetching prices for {len(missing)} tickers missing data: {missing}')
            for t in missing:
                fetch_and_store_prices(t, days_back=365)
            log('INFO', 'trading_agents', 'Price pre-fetch complete')

        raw_signals = compute_signals_batch(tickers)
        signals = {t: {'conviction': float(v)} for t, v in raw_signals.items()}
        results = analyze_daily_set(daily_set, signals)
        save_recommendations(results)
        for t, d in results.items():
            print(f'\n{t}: {d["action"]} conf={d["confidence"]:.2f}')
            print(f'  Reasoning: {d["reasoning"][:120]}')
            print(f'  Risk: {d["risk_notes"][:80]}')
