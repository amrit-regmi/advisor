"""
Rate Limit Manager — Groq + OpenRouter budget tracking.

Free-tier limits per API key:
  Groq       — 100,000 tokens/day,  28 RPM  (llama-3.3-70b-versatile)
  OpenRouter — 50 :free req/day,    20 RPM

Per-ticker pipeline cost:
  Groq       — ~4,000 tokens  (7 calls: analysts × 2 + debate × 4 + evaluator + trader)
  OpenRouter — 2 calls         (Risk Analyst + Portfolio Manager)

Daily capacity with 2 keys each:
  Groq       — 200,000 tokens  → ~50 tickers
  OpenRouter — 100 requests    → 44 tickers (daily20 pool) + 6 on-demand

Adding a key to .env (e.g. GROQ_API_KEY_2) automatically scales the budget.

State is persisted to rate_limit_state.json so a process restart mid-pipeline
doesn't lose the daily counter and accidentally double-spend OR quota.
"""
import os
import sys
import time
import threading
from datetime import date
from pathlib import Path
import json

sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import log

# ── Per-key free-tier limits ──────────────────────────────────────────────────
_GROQ_DAILY_PER_KEY         = 100_000   # tokens/day
_OR_DAILY_PER_KEY           = 50        # :free model requests/day
GROQ_RPM_LIMIT              = 28        # requests per minute (Groq free)
OPENROUTER_RPM_LIMIT        = 20        # requests per minute (OR :free)

# Observed production cost per ticker (not theoretical minimums)
# OR calls: Risk Manager(1) + PM Response(1) + RM Rebuttal(1) + PM Final(1) = 4 per ticker
GROQ_TOKENS_PER_TICKER      = 5_200
OPENROUTER_CALLS_PER_TICKER = 4


def _count_unique_keys(env_prefix: str) -> int:
    """Count distinct non-empty values for env-vars starting with env_prefix.

    Deduplicates by value so the same key stored under two names (e.g.
    GROQ_API_KEY and GROQ) is counted once.
    """
    from dotenv import dotenv_values
    file_env = dotenv_values('/home/ubuntu/advisor/.env')
    combined = {**file_env, **os.environ}   # os.environ wins on conflict
    seen = set()
    for name, val in combined.items():
        if name.startswith(env_prefix) and val and val.strip():
            seen.add(val.strip())
    return max(1, len(seen))   # never return 0 — avoids division-by-zero downstream


def _compute_limits():
    """Compute daily limits scaled by key count. Called once at import time."""
    or_keys   = _count_unique_keys('OPENROUTER')
    groq_keys = _count_unique_keys('GROQ')

    total_or = _OR_DAILY_PER_KEY * or_keys
    # Reserve 89% for the scheduled morning run; 11% for on-demand dashboard queries.
    # This prevents a user triggering on-demand analysis from starving the daily pipeline.
    daily20  = max(10, int(total_or * 0.89))
    ondemand = total_or - daily20

    return {
        'groq_daily':     _GROQ_DAILY_PER_KEY * groq_keys,
        'or_total':       total_or,
        'or_daily20':     daily20,
        'or_ondemand':    ondemand,
        'or_key_count':   or_keys,
        'groq_key_count': groq_keys,
    }


_LIMITS = _compute_limits()

GROQ_DAILY_LIMIT          = _LIMITS['groq_daily']
OPENROUTER_DAILY_TOTAL    = _LIMITS['or_total']
OPENROUTER_DAILY_DAILY20  = _LIMITS['or_daily20']
OPENROUTER_DAILY_ONDEMAND = _LIMITS['or_ondemand']

DAILY_TICKER_LIMIT     = OPENROUTER_DAILY_DAILY20  // OPENROUTER_CALLS_PER_TICKER
ON_DEMAND_TICKER_LIMIT = max(2, OPENROUTER_DAILY_ONDEMAND // OPENROUTER_CALLS_PER_TICKER)

STATE_FILE = Path('/home/ubuntu/advisor/data/rate_limit_state.json')


class RateLimitManager:
    def __init__(self):
        self._lock           = threading.Lock()
        self._state          = self._load_state()
        self._groq_min_calls = []   # timestamps of Groq calls within the current 60s window
        self._or_min_calls   = []   # same for OpenRouter

    def _load_state(self) -> dict:
        """Load today's counters from disk, or start fresh if date changed."""
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fresh = {
            'date': str(date.today()),
            'groq_tokens_used': 0,
            'openrouter_daily20_used': 0,
            'openrouter_ondemand_used': 0,
            'tickers_daily20_done': 0,
            'tickers_ondemand_done': 0,
        }
        if STATE_FILE.exists():
            try:
                s = json.loads(STATE_FILE.read_text())
                if s.get('date') == str(date.today()):
                    # Migrate old single-pool format written before the dual-pool redesign
                    if 'openrouter_daily20_used' not in s:
                        old_or = s.get('openrouter_requests_used', 0)
                        s['openrouter_daily20_used'] = old_or
                        s['openrouter_ondemand_used'] = 0
                    fresh.update(s)
                    return fresh
            except Exception:
                pass
        return fresh

    def _save(self):
        STATE_FILE.write_text(json.dumps(self._state))

    def _prune(self, calls: list) -> list:
        """Drop timestamps older than 60 seconds (outside the current RPM window)."""
        now = time.time()
        return [t for t in calls if now - t < 60]

    def check_groq_tokens(self, tokens: int = 200):
        """
        Gate a Groq call: enforce daily token budget and 28 RPM limit.

        If the RPM window is full, sleeps until the oldest call exits the
        60-second window (with a 2s buffer to avoid boundary edge cases).
        """
        with self._lock:
            if self._state['groq_tokens_used'] + tokens > GROQ_DAILY_LIMIT:
                raise RuntimeError(
                    f"Groq daily limit: {self._state['groq_tokens_used']}/{GROQ_DAILY_LIMIT}")
            self._groq_min_calls = self._prune(self._groq_min_calls)
            if len(self._groq_min_calls) >= GROQ_RPM_LIMIT:
                # Wait until the oldest call in the window is >60s old
                wait = 62 - (time.time() - self._groq_min_calls[0])
                if wait > 0:
                    log('rate_limit', 'info', f'Groq RPM — waiting {wait:.0f}s')
                    time.sleep(wait)
                self._groq_min_calls = self._prune(self._groq_min_calls)
            self._groq_min_calls.append(time.time())
            self._state['groq_tokens_used'] += tokens
            self._save()

    def check_openrouter_requests(self, pool: str = 'daily20'):
        """
        RPM throttle for OpenRouter — called before each OR attempt.

        Note: this does NOT increment the daily counter. Call record_or_call()
        only after the request returns content, so failed/retried calls don't
        consume daily quota.
        """
        with self._lock:
            self._or_min_calls = self._prune(self._or_min_calls)
            if len(self._or_min_calls) >= OPENROUTER_RPM_LIMIT:
                wait = 62 - (time.time() - self._or_min_calls[0])
                if wait > 0:
                    log('rate_limit', 'info', f'OpenRouter RPM — waiting {wait:.0f}s')
                    time.sleep(wait)
                self._or_min_calls = self._prune(self._or_min_calls)
            self._or_min_calls.append(time.time())

    def record_or_call(self, pool: str = 'daily20'):
        """Increment the daily OR counter. Call only when OR returns actual content."""
        with self._lock:
            used_key = f'openrouter_{pool}_used'
            self._state[used_key] = self._state.get(used_key, 0) + 1
            self._save()

    def can_run_ticker(self, pool: str = 'daily20') -> bool:
        """Return True if both Groq and OR budgets can cover one more full ticker."""
        groq_ok = (self._state['groq_tokens_used'] + GROQ_TOKENS_PER_TICKER
                   <= GROQ_DAILY_LIMIT)
        if pool == 'daily20':
            or_ok = (self._state['openrouter_daily20_used'] + OPENROUTER_CALLS_PER_TICKER
                     <= OPENROUTER_DAILY_DAILY20)
        else:
            # On-demand checks total OR spend (daily20 + ondemand combined)
            total_used = (self._state['openrouter_daily20_used']
                          + self._state['openrouter_ondemand_used'])
            or_ok = total_used + OPENROUTER_CALLS_PER_TICKER <= OPENROUTER_DAILY_TOTAL
        return groq_ok and or_ok

    def record_ticker_done(self, pool: str = 'daily20'):
        with self._lock:
            key = 'tickers_daily20_done' if pool == 'daily20' else 'tickers_ondemand_done'
            self._state[key] = self._state.get(key, 0) + 1
            self._save()

    def status(self) -> dict:
        d20_used = self._state['openrouter_daily20_used']
        od_used  = self._state['openrouter_ondemand_used']
        total_or = d20_used + od_used
        return {
            'groq_tokens_used':          self._state['groq_tokens_used'],
            'groq_tokens_remaining':     GROQ_DAILY_LIMIT - self._state['groq_tokens_used'],
            'or_daily20_used':           d20_used,
            'or_daily20_remaining':      OPENROUTER_DAILY_DAILY20 - d20_used,
            'or_ondemand_used':          od_used,
            'or_ondemand_remaining':     max(0, OPENROUTER_DAILY_ONDEMAND - od_used),
            'or_total_used':             total_or,
            'or_total_remaining':        OPENROUTER_DAILY_TOTAL - total_or,
            'tickers_daily20_done':      self._state.get('tickers_daily20_done', 0),
            'tickers_ondemand_done':     self._state.get('tickers_ondemand_done', 0),
            'daily_ticker_limit':        DAILY_TICKER_LIMIT,
            'ondemand_ticker_remaining': max(0, (OPENROUTER_DAILY_ONDEMAND - od_used)
                                             // OPENROUTER_CALLS_PER_TICKER),
        }


_manager = None


def get_manager() -> RateLimitManager:
    global _manager
    if _manager is None:
        _manager = RateLimitManager()
    return _manager
