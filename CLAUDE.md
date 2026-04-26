# AI Financial Advisor — Build Instructions

## What we're building
A personal AI financial advisor that:
- Proactively discovers investment opportunities from global events
- Monitors existing holdings for sell signals
- Delivers BUY/SELL/WATCH recommendations via Telegram at 7am
- Accounts for Finnish tax law and MiFID II regulations
- Runs on Oracle ARM VM at zero ongoing cost

## You have full freedom
Refactor, improve, restructure or rewrite any existing code
if you think it makes the system better. The goal is a working
system — not preserving any particular implementation.

## Environment
- OS: Ubuntu 22.04 ARM64
- Python venv: ~/venv/
- Working directory: ~/advisor/
- All secrets in: ~/advisor/.env
- PostgreSQL running on localhost

## Credentials — always from .env, never hardcoded
```python
import os
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/advisor/.env')
# Available keys:
# DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
# ALPHA_VANTAGE_KEY, FRED_API_KEY
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# GROQ_API_KEY, GROQ (same key, both names present)
# OPENROUTER_API_KEY, OPENROUTER (same key, both names present)
```

## Database — always use these helpers
```python
import sys
sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import execute, query, log, can_use_alpha_vantage
```

## Database schema (actual columns)
- prices: ticker, date, open, high, low, close, volume, created_at
- news_sentiment: ticker, date, article_count, avg_tone, top_themes, mention_velocity, tone_trajectory, is_accelerating, finbert_sentiment_score, finbert_sentiment_label, finbert_confidence, positive_count, negative_count, sector, source_countries, created_at
- macro_data: series_id, date, value, created_at  ← NOT "indicator"
- polymarket_signals: market_id, question, probability, relevant_tickers, relevant_sectors, date, created_at
- holdings: ticker, shares, avg_buy_price, exchange, currency, notes, active, bought_date, created_at, updated_at  ← NOT "quantity"
- watchlist: ticker, company_name, active, created_at  ← NOT "is_active", NOT "added_date"
- universe: ticker, company_name, exchange, sector, country, asset_type, active, validated, created_at, updated_at  ← NOT "is_active"
- discovery_candidates: ticker, direction, event_type, reason, total_score, gdelt_score, polymarket_score, momentum_score, quant_score, blended_score, eligible, selected_for_analysis, analyzed, dividend_withholding_pct, currency, fx_risk, tax_notes, date, created_at  ← NOT "score"
- recommendations: ticker, action, confidence, reasoning, date, bull_case, bear_case, key_risks, time_horizon, signal_sources, debate_trace, acted_on, created_at
- user_settings: key, value, created_at, updated_at  ← key-value store, NOT direct columns

---

## ARCHITECTURE (15 phases)

### Phase 0 — Global System Layer
- LLM routing: Groq llama-3.3-70b-versatile (fast agents), OpenRouter nvidia/nemotron-3-super-120b-a12b:free (deep reasoning)
- Groq free tier: 12k TPM for 70B — use compressed prompts (≤3000 chars input, ≤400 tokens output)
- OpenRouter free tier: 45 requests/day — used only for Evaluator + Risk Analyst (2 per ticker × 16 = 32/day)
- Rate limit manager: analysis/rate_limit_manager.py
- TradingAgents debate: implemented directly in analysis/trading_agents_wrapper.py (not using TradingAgents.propagate() due to token constraints)

### Phase 1 — Universe Construction
- Source: adanos-software/free-ticker-database (GitHub) — 61k+ tickers
- File: pipeline/universe_loader.py
- Refresh: monthly download, weekly yfinance validation
- Target exchanges: NASDAQ, NYSE, AMEX, XETRA, LSE, SIX, HEL, STO, CPH, First North
- Cache: ~/advisor/data/universe_cache/

### Phase 2 — Data Collection
- pipeline/collect_all.py — master runner
- pipeline/gdelt_collector.py — GDELT news sentiment, every 15 min
- pipeline/fred_collector.py — FRED macro data daily
- pipeline/polymarket_collector.py — Polymarket signals every 6 hours
- pipeline/price_collector.py — yfinance prices daily

### Phase 3 — Sentiment Engine
- pipeline/finbert_sentiment.py — ProsusAI/finbert local model
- Scores GDELT news tone with FinBERT: finbert_today, finbert_3d_avg, finbert_7d_avg
- Composite sentiment from GDELT tone + FinBERT scores

### Phase 4 — Signal Engine
- pipeline/signal_engine.py — conviction scores per ticker
- Components: momentum_7d, sentiment_avg, fundamentals, catalyst, macro_alignment, volatility_penalty, multi_confirm
- Output: conviction[ticker] in [0, 1]

### Phase 5 — Backtest Engine (offline)
- pipeline/backtester.py — offline validation
- Uses Backtrader, runs historical simulations

### Phase 6 — State Machine
- Holdings: max 10 positions, tracked in holdings table
- Watchlist: medium conviction names, tracked in watchlist table
- Discovery memory: tracked in discovery_candidates table (penalty_score, cooldown via blended_score)

### Phase 7 — Discovery Engine
- pipeline/discovery/event_classifier.py — detects events from GDELT
- pipeline/discovery/event_mapper.py — maps events to sectors/stocks
- pipeline/discovery/scorer.py — scores candidates 0-100
- pipeline/tax_filter.py — Finnish MiFID II filter

### Phase 8 — Daily-16 Selection
- pipeline/daily_selection.py
- Priority: holdings → watchlist top-3 → strong overrides (conviction≥0.70) → discovery 2 → rotation fill
- Portfolio full (10 holdings) → no discovery

### Phase 9 — Portfolio Snapshot
- Built inside pipeline/context_builder.py
- Tracks n_holdings, cash_eur, sector/country exposure, constraints

### Phase 10 — Context Builder
- pipeline/context_builder.py — assembles per-ticker context
- Pulls signals, FinBERT sentiment, discovery/watchlist metadata, portfolio snapshot

### Phase 11 — Rate Limit Manager
- analysis/rate_limit_manager.py
- Tracks Groq token usage (budget: 95k/day) and OpenRouter requests (budget: 45/day)
- Persists state in ~/advisor/data/rate_limit_state.json

### Phase 12 — Agent Debate (TradingAgents pattern)
- analysis/trading_agents_wrapper.py
- 6 agents: Market Analyst → News Analyst → Bear Researcher → Bull Researcher → Research Evaluator (Nvidia) → Trader → Risk Analyst (Nvidia) → Portfolio Manager
- Each Groq call: prompt capped at 3000 chars, output at 300-400 tokens
- Entry: analyze_ticker(ticker, state, signals) → pm_final_decision dict
- Schema: {action, sizing_intent, watchlist_action, rotation_flag, strong_override, risk_notes, confidence, reasoning}

### Phase 13 — Global Reconciliation
- portfolio/reconciliation.py
- Converts per-ticker decisions into portfolio-level trade_plan
- Applies: holdings constraints (max 10), sector limit (25%), country limit (40%), rotation rules, watchlist actions

### Phase 14 — Portfolio Optimizer
- portfolio/optimizer.py — PyPortfolioOpt
- Action → weight mapping: STRONG_BUY=6-12%, BUY=3-8%, HOLD=maintain, SELL=0%
- Constraints: sum=100%, cash≥5%, max_position=12%, max_sector=25%

### Phase 15 — Execution + Delivery
- portfolio/portfolio_brain.py — final recommendations
- portfolio/holdings_monitor.py — monitors existing holdings
- alerts/telegram_bot.py — morning brief + alerts
- dashboard/app.py — Flask UI (port 5000, bind 0.0.0.0)

---

## Pipeline execution order (run_daily.sh)
1. collect_all.py
2. universe_loader.py (monthly; skip with SKIP_UNIVERSE=1)
3. event_classifier.py
4. event_mapper.py
5. scorer.py
6. tax_filter.py
7. daily_selection.py
8. trading_agents_wrapper.py (analyze_daily_selection)
9. reconciliation.py
10. optimizer.py
11. holdings_monitor.py
12. portfolio_brain.py
13. sanity_check.py
14. performance_tracker.py

---

## General rules
- Read all existing code before writing anything new
- No hardcoded credentials, tickers, or company names anywhere
- Use log() from db/database.py for all logging
- Always use the actual DB schema columns listed above
- SELL only if stock exists in holdings table (active=true AND shares>0)
- Dashboard must bind to 0.0.0.0 not 127.0.0.1
- Groq prompt input: ≤3000 chars. Output: ≤400 tokens (free tier constraint)
- OpenRouter: max 2 calls per ticker, 45 calls total per day



Updated artichcture version! Priority align with below if conflict!  


┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 0. GLOBAL SYSTEM LAYER                                                                    │
│   - Model routing:                                                                         │
│       • Groq Llama 3.1 70B for fast reasoning agents                                       │
│       • OpenRouter Nvidia models for deep reasoning agents, for example:                   │
│           - Nvidia Nemotron 3 70B                                                          │
│   - Debate configuration:                                                                  │
│       • Four‑round Bull versus Bear research debate                                        │
│       • Risk Analyst and Portfolio Manager arbitration rounds                              │
│   - TradingAgents by TauricResearch is the only Large Language Model orchestration layer   │
│   - Dynamic rate‑limit handling for all Large Language Model calls                         │
└──────────────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. UNIVERSE CONSTRUCTION                                                                  │
│                                                                                            │
│   PURPOSE: Build a clean, validated, global equity universe (~1000 tickers) 

    - Supported exchanges:                                                                   │
│       • NASDAQ                                                                             │
│       • New York Stock Exchange                                                            │
│       • American Stock Exchange                                                            │
│       • XETRA                                                                              │
│       • London Stock Exchange                                                              │
│       • SIX Swiss Exchange                                                                 │
│       • Helsinki Exchange                                                                  │
│       • Stockholm Exchange                                                                 │
│       • Copenhagen Exchange                                                                │
│       • First North Exchange                                                               │
│   - Validate tickers using Yahoo Finance                                   │
│                                                                                            │
│   1.1 PRIMARY FREE DATA SOURCE                                                             │
│       • Free Global Ticker Database (GitHub)                                               │
│         https://github.com/adanos-software/free-ticker-database                            │
│         - 61,000+ tickers across 67 exchanges and 56 countries                             │
│         - Includes: ticker, name, exchange, sector, country, ISIN, aliases                 │
│         - Formats: CSV, JSON, Parquet, SQLite                                              │
│         - Updated frequently                                                               │
│                                                                                            │
│       WHY THIS IS THE BEST SOURCE:                                                         │
│         - Covers all exchanges you need (NASDAQ, NYSE, XETRA, LSE, SIX, Helsinki, etc.)   │
│         - Includes sector and country metadata                                             │
│         - Free, no API key, no rate limits                                                 │
│                                                                                            │
│   1.2 SECONDARY VALIDATION SOURCES (FREE)                                                  │
│       • Yahoo Finance (query1.finance.yahoo.com)                                           │
│         - Validate ticker exists and is active                                             │
│         - Validate sector, country, and listing status                                     │
│                                                                                            │
│       • Stooq (stooq.com)                                                                  │
│         - Free listings for US, UK, EU, Japan, Hong Kong                                   │
│         - Good for cross‑checking delisted or inactive tickers                             │
│                                                                                            │
│   1.3 OPTIONAL SUPPLEMENTARY SOURCES (FREE)                                                │
│       • Marketstack free tier                                                              │
│       • Alpha Vantage free tier                                                            │
│       • Finnhub free tier                                                                  │
│       (Used only if you want additional metadata)                                          │
│                                                                                            │
│   1.4 FINLAND‑SPECIFIC TAX FILTER                                                          │
│       • Exclude all United States‑domiciled Exchange‑Traded Funds                          │
│       • Replace with European Union UCITS equivalents                                      │
│       • Add metadata: withholding tax, domicile, UCITS flag                                │
│                                                                                            │
│   1.5 REFRESH FREQUENCY                                                                    │
│       • Free Global Ticker Database: once per month                                        │
│       • Yahoo Finance validation: once per week                                            │
│       • Stooq cross‑check: once per month                                                  │
│                                                                                            │
│       REASON:                                                                              │
│         - Listings do not change daily                                                     │
│         - Monthly refresh is enough for new listings, delistings, mergers                  │
│                                                                                            │
│   1.6 OUTPUT                                                                                │
│       • universe_clean (approximately one thousand validated tickers)                      │
│       • Each entry includes:                                                               │
│           - ticker                                                                          │
│           - name                                                                            │
│           - exchange                                                                        │
│           - sector                                                                          │
│           - country                                                                         │
│           - ISIN                                                                            │
│           - UCITS eligibility                                                               │
│           - Finland tax compliance                                                          │
└──────────────────────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. DATA COLLECTION LAYER (FREE SOURCES + REFRESH FREQUENCY)                               │
│                                                                                           │
│   2.1 Yahoo Finance (FREE)                                                                │
│       • Source: https://query1.finance.yahoo.com                                          │
│       • Data: price history, volume, dividends, splits, fundamentals                      │
│       • Frequency:                                                                          │
│           - Price data: once per day (after market close)                                 │
│           - Fundamentals: once per week                                                   │
│       • Reason: Yahoo updates end‑of‑day data reliably; fundamentals update slowly         │
│                                                                                           │
│   2.2 GDELT Global News Database (FREE)                                                   │
│       • Source: https://api.gdeltproject.org                                              │
│       • Data: global news metadata, tone, event categories                                │
│       • Frequency:                                                                          │
│           - Every 15 minutes (GDELT updates continuously)                                 │
│       • Reason: sentiment and catalysts can shift intraday                                │
│                                                                                           │
│   2.3 FinBERT Sentiment Model (FREE, LOCAL)                                               │
│       • Source: HuggingFace model "ProsusAI/finbert"                                      │
│       • Data: sentiment on news headlines and summaries                                   │
│       • Frequency:                                                                          │
│           - Run whenever new GDELT articles appear                                        │
│           - Minimum: once per day                                                         │
│       • Reason: sentiment decays quickly; daily smoothing needed                          │
│                                                                                           │
│   2.4 Federal Reserve Economic Data (FRED) (FREE)                                         │
│       • Source: https://fred.stlouisfed.org                                               │
│       • Data: macroeconomic indicators (rates, inflation, unemployment)                   │
│       • Frequency:                                                                          │
│           - Once per day (morning)                                                        │
│       • Reason: macro data updates slowly; daily pull is enough                           │
│                                                                                           │
│   2.5 Polymarket Event Probability API (FREE)                                             │
│       • Source: https://polymarket.com/api                                                │
│       • Data: event probabilities (elections, macro events, company events)               │
│       • Frequency:                                                                          │
│           - Every 6 hours                                                                 │
│       • Reason: event probabilities shift intraday but not minute‑to‑minute               │
│                                                                                           │
│   2.6 Corporate Catalyst Calendar (FREE SOURCES)                                          │
│       • Sources:                                                                           │
│           - Yahoo Finance earnings calendar                                               │
│           - Nasdaq earnings calendar                                                      │
│           - Finnhub free tier (optional)                                                  │
│       • Data: earnings dates, guidance, dividends, splits                                 │
│       • Frequency:                                                                          │
│           - Once per day                                                                  │
│       • Reason: catalysts rarely change intraday                                          │
│                                                                                           │
│   OUTPUT: raw_data[ticker]                                                                │
│                                                                                           │
│   NOTE: All sources above are 100% free and require no paid API keys.                     │
└──────────────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 3. SENTIMENT ENGINE (FREE SOURCES + REFRESH FREQUENCY)                                   │
│                                                                                           │
│   PURPOSE: Produce a multi‑layer sentiment profile for every ticker                       │
│                                                                                           │
│   3.1 FinBERT Sentiment (FREE, LOCAL MODEL)                                               │
│       • Source: HuggingFace model "ProsusAI/finbert"                                      │
│       • Input: news headlines and summaries from GDELT                                    │
│       • Output:                                                                            │
│           - finbert_today                                                                  │
│           - finbert_three_day_average                                                      │
│           - finbert_seven_day_average                                                      │
│       • Frequency:                                                                          │
│           - Run every time new GDELT articles appear                                       │
│           - Minimum: every fifteen minutes                                                 │
│       • Reason: sentiment decays quickly; smoothing requires frequent updates              │
│                                                                                           │
│   3.2 GDELT Global Tone (FREE)                                                            │
│       • Source: https://api.gdeltproject.org                                              │
│       • Data: tone, sentiment polarity, event categories                                  │
│       • Frequency:                                                                          │
│           - Every fifteen minutes (GDELT updates continuously)                             │
│       • Reason: global news sentiment shifts intraday                                      │
│                                                                                           │
│   3.3 News Sentiment (FREE)                                                               │
│       • Source: GDELT article metadata + FinBERT scoring                                  │
│       • Data: sentiment of financial news headlines                                        │
│       • Frequency:                                                                          │
│           - Every fifteen minutes                                                          │
│       • Reason: news flow is volatile and impacts conviction                               │
│                                                                                           │
│   3.4 Social Media Sentiment (FREE)                                                       │
│       • Source: Reddit and Twitter/X public endpoints via free scrapers                    │
│         (no paid API required; use open‑source scrapers)                                   │
│       • Data: ticker mentions, sentiment, volume                                           │
│       • Frequency:                                                                          │
│           - Every thirty minutes                                                           │
│       • Reason: social sentiment is noisy but useful for trend confirmation                │
│                                                                                           │
│   3.5 Composite Sentiment Score                                                            │
│       • Formula:                                                                            │
│           composite_sentiment = weighted_average(                                          │
│               finbert_today,                                                               │
│               finbert_three_day_average,                                                   │
│               finbert_seven_day_average,                                                   │
│               gdelt_tone,                                                                  │
│               news_sentiment,                                                              │
│               social_media_sentiment                                                       │
│           )                                                                                │
│       • Frequency:                                                                          │
│           - Recomputed every fifteen minutes                                               │
│       • Reason: ensures consistency across all sentiment sources                           │
│                                                                                           │
│   OUTPUT: sentiment[ticker]                                                                │
│                                                                                           │
│   NOTE: All sentiment sources above are 100% free and require no paid API keys.            │
└──────────────────────────────────────────────────────────────────────────────────────────┘



┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 4. SIGNAL ENGINE                                                                          │
│   - Seven‑day momentum                                                                    │
│   - Seven‑day sentiment average                                                            │
│   - Seven‑day fundamentals trend                                                           │
│   - Seven‑day catalyst score                                                               │
│   - Macroeconomic alignment                                                                │
│   - Volatility score                                                                       │
│   - Risk flags                                                                             │
│   - Conviction score = function of all signals                                             │
│   OUTPUT: signals[ticker], conviction[ticker]                                              │
└──────────────────────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 5. BACKTEST ENGINE (OFFLINE RESEARCH & VALIDATION LAYER)                                  │
│                                                                                            │
│   PURPOSE:                                                                                 │
│       Provide historical validation of the full trading pipeline, including:               │
│           • Signal Engine                                                                   │
│           • TradingAgents reasoning                                                         │
│           • Portfolio Manager decisions                                                     │
│           • Optimizer weight generation                                                     │
│           • Execution logic                                                                 │
│                                                                                            │
│       This layer is OFFLINE and does NOT run daily.                                        │
│       It is used for:                                                                      │
│           • Strategy validation                                                             │
│           • Performance measurement                                                         │
│           • Regime testing                                                                  │
│           • Drawdown analysis                                                               │
│           • Turnover analysis                                                               │
│           • Risk diagnostics                                                                │
│                                                                                            │
│   TOOLING:                                                                                 │
│       • Backtrader (primary backtest engine)                                               │
│       • Optional: Qlib for factor research (not required for live trading)                 │
│                                                                                            │
│   INPUTS:                                                                                  │
│       • Historical price data                                                               │
│       • Historical sentiment (FinBERT/GDELT)                                               │
│       • Historical fundamentals                                                             │
│       • Historical macro data                                                               │
│       • Historical signals                                                                  │
│                                                                                            │
│   PIPELINE SIMULATION:                                                                     │
│       For each historical day:                                                             │
│           1. Compute signals                                                               │
│           2. Run TradingAgents (offline mode)                                              │
│           3. Generate PM decision                                                          │
│           4. Convert to target weights                                                     │
│           5. Simulate execution                                                            │
│           6. Update portfolio state                                                        │
│                                                                                            │
│   METRICS PRODUCED:                                                                        │
│       • CAGR                                                                                │
│       • Sharpe ratio                                                                        │
│       • Sortino ratio                                                                       │
│       • Max drawdown                                                                        │
│       • Volatility                                                                          │
│       • Turnover                                                                            │
│       • Hit rate                                                                            │
│       • Win/loss distribution                                                               │
│       • Regime performance (bull, bear, sideways)                                          │
│                                                                                            │
│   OUTPUT: backtest_results                                                                  │
│       {                                                                                    │
│         "performance": {...},                                                              │
│         "risk": {...},                                                                     │
│         "turnover": {...},                                                                 │
│         "regime_analysis": {...},                                                          │
│         "daily_equity_curve": [...],                                                       │
│         "trade_log": [...]                                                                 │
│       }                                                                                    │
│                                                                                            │
│   EFFECT ON SYSTEM:                                                                        │
│       • Validates strategy before deployment                                               │
│       • Detects weaknesses in signals or reasoning                                         │
│       • Provides long‑run performance expectations                                         │
│       • Informs penalty/cooldown rules                                                     │
│       • Informs discovery engine scoring                                                   │
│       • Informs optimizer constraints                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 6. STATE MACHINE (HOLDINGS, WATCHLIST, DISCOVERY, STRONG OVERRIDES)                       │
│   - Holdings:                                                                              │
│       • Maximum ten positions                                                              │
│       • Each holding has weight, sector, country, entry date                               │
│   - Watchlist:                                                                             │
│       • Contains medium conviction names and manual additions                              │
│       • Tracks: days on watchlist, stagnation days, last conviction, last catalyst         │
│   - Discovery memory:                                                                      │
│       • Tracks: last_seen_date, penalty_score, cooldown_days, novelty_score                │
│   - Strong overrides:                                                                      │
│       • Tickers with conviction greater than or equal to zero point seven zero             │
│   - Watchlist exit rules:                                                                  │
│       • Remove if conviction below zero point five five for two consecutive days           │
│       • Remove if no improvement for fourteen days                                         │
│       • Remove on strong negative catalyst                                                 │
│       • Remove if promoted to holding or strong override                                   │
│       • Remove if penalty threshold exceeded                                               │
│   OUTPUT: updated state                                                                    │
└──────────────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 7. DISCOVERY ENGINE (DETAILED LOGIC)                                                      │
│   - Input universe: universe_clean                                                         │
│   - Exclusions:                                                                            │
│       • Exclude current holdings                                                           │
│       • Exclude current watchlist names                                                    │
│       • Exclude tickers in cooldown (discovery memory with cooldown_days > 0)             │
│   - Scoring:                                                                              │
│       • Base score = conviction[ticker]                                                    │
│       • Add novelty_score from discovery memory                                            │
│       • Subtract penalty_score from discovery memory                                       │
│   - Selection:                                                                            │
│       • Sort by combined discovery score                                                   │
│       • Select up to two new discovery candidates per day                                  │
│       • Update discovery memory: last_seen_date, penalty_score, cooldown_days              │
│   OUTPUT: discovery_candidates                                                             │
└──────────────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 8. DAILY SIXTEEN SELECTION PIPELINE                                                       │
│   Priority order:                                                                          │
│       1. Holdings (all current holdings)                                                   │
│       2. Watchlist (top three by conviction)                                               │
│       3. Strong conviction names (conviction ≥ 0.70, up to three)                          │
│       4. Discovery candidates (up to two)                                                  │
│       5. Rotation fill (remaining slots to reach sixteen tickers)                          │
│   Portfolio full condition (ten holdings):                                                 │
│       • No discovery candidates allowed                                                    │
│   OUTPUT: DAILY_SIXTEEN_SET (exactly sixteen tickers)                                      │
└──────────────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 9. PORTFOLIO SNAPSHOT                                                                     │
│   - Current holdings and weights                                                           │
│   - Cash weight                                                                            │
│   - Sector exposure                                                                        │
│   - Geographic exposure                                                                    │
│   - Portfolio constraints:                                                                 │
│       • Maximum number of holdings                                                         │
│       • Maximum sector weight                                                              │
│       • Maximum single‑position weight                                                     │
│       • Maximum turnover                                                                   │
│   - Portfolio risk flags                                                                   │
│   OUTPUT: portfolio_snapshot                                                               │
└──────────────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 10. CONTEXT BUILDER (DISCOVERY + SENTIMENT + PORTFOLIO INJECTION)                          │
│   For each ticker in DAILY_SIXTEEN_SET:                                                    │
│       • signals[ticker]                                                                    │
│       • conviction[ticker]                                                                 │
│       • raw_data_summary                                                                   │
│       • sentiment[ticker]                                                                  │
│       • ticker_state (holding, watchlist, discovery, strong override)                      │
│       • discovery_metadata (from discovery memory)                                         │
│       • watchlist_metadata (from watchlist state)                                          │
│       • strong_override flag                                                               │
│       • portfolio_snapshot                                                                 │
│   OUTPUT: context[ticker]                                                                  │
└──────────────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 11. DYNAMIC RATE‑LIMIT MANAGER (GROQ + OPENROUTER)                                        │
│                                                                                            │
│   PURPOSE: Ensure TradingAgents runs reliably without exceeding Groq token limits or       │
│            OpenRouter request limits during DAILY‑SIXTEEN execution.                       │
│                                                                                            │
│   WHAT IT MANAGES:                                                                         │
│       • Groq token usage (≤ 100,000 tokens/day free tier)                                  │
│       • OpenRouter request usage (≤ 50 requests/day free tier)                             │
│       • Per‑minute throughput (Groq TPM, OpenRouter 20 RPM)                                │
│       • Retry logic for 429 errors                                                         │
│       • Scheduling of tradingagents.run() calls                                            │
│                                                                                            │
│   WHAT IT DOES:                                                                            │
│       • Queues LLM calls to avoid exceeding per‑minute limits                              │
│       • Spaces calls to ensure Groq token budget is not exceeded                           │
│       • Tracks OpenRouter request count (max 2 per ticker × 16 = 32/day)                   │
│       • Predicts token usage for Groq‑routed agents                                        │
│       • Retries failed calls with exponential backoff                                      │
│       • Prevents partial DAILY‑SIXTEEN runs                                                │
│       • Ensures deterministic, safe execution                                              │
│                                                                                            │
│   WHAT IT DOES NOT DO:                                                                     │
│       • Increase Groq or OpenRouter limits                                                 │
│       • Interfere with internal TradingAgents agent calls                                  │
│                                                                                            │
│   EXECUTION WRAPPER:                                                                       │
│       for ticker in DAILY_SIXTEEN_SET:                                                     │
│           rate_limit_manager.check_groq_tokens()                                           │
│           rate_limit_manager.check_openrouter_requests()                                   │
│           rate_limit_manager.check_per_minute_capacity()                                   │
│                                                                                            │
│           result[ticker] = tradingagents.run(ticker, context[ticker])                      │
│                                                                                            │
│   NOTES:                                                                                   │
│       • The rate‑limit manager controls WHEN tradingagents.run() is called,                │
│         not the internal LLM calls inside TradingAgents.                                   │
│                                                                                            │
│       • With the updated model routing (Groq for 10 calls/ticker, Nvidia for 2 calls),     │
│         DAILY‑SIXTEEN stays within both free‑tier limits when Groq prompts are compressed. │
│                                                                                            │
│   OUTPUT:                                                                                  │
│       • Safe, scheduled execution of all TradingAgents runs                                │
│       • No rate‑limit violations                                                           │
│       • No partial or failed DAILY‑SIXTEEN runs                                            │
└──────────────────────────────────────────────────────────────────────────────────────────┘



┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 12. TRADINGAGENTS ORCHESTRATION LAYER (SINGLE ENTRY POINT + FREE‑TIER MODEL ROUTING)      │
│                                                                                            │
│   ENTRY POINT (ONLY ONE):                                                                  │
│       result[ticker] = tradingagents.run(ticker, context[ticker])                          │
│                                                                                            │
│   All agents below are INTERNAL roles defined in the TradingAgents configuration file.     │
│   Model routing and token budgets are optimized to stay within BOTH Groq and OpenRouter    │
│   free‑tier limits for DAILY‑SIXTEEN execution.                                            │
│                                                                                            │
│   12.0 CONFIGURATION REQUIREMENT (CRITICAL)                                                │
│       Create: tradingagents_config.yaml                                                    │
│       This file defines:                                                                   │
│           • Agent roles                                                                    │
│           • System prompts                                                                 │
│           • JSON schemas                                                                   │
│           • Model assignments (Groq + Nvidia)                                              │
│           • Debate rounds                                                                  │
│           • Arbitration logic                                                              │
│                                                                                            │
│       FREE‑TIER TOKEN CONSTRAINTS:                                                         │
│           • Groq free tier: maximum ~100,000 tokens/day                                    │
│           • OpenRouter free tier: maximum 50 requests/day                                  │
│                                                                                            │
│       PROMPT COMPRESSION REQUIREMENT:                                                      │
│           • All Groq‑routed agents MUST use compressed prompts                             │
│             (target ≤ 600–900 tokens per call including prompt + context + output).        │
│           • This ensures total Groq usage stays below 100k/day for DAILY‑16.               │
│                                                                                            │
│   12.1 ANALYST AGENTS (Groq Llama 3.1 70B — FREE‑TIER SAFE)                                │
│       • Market Analyst                                                                     │
│       • Social Media Analyst                                                               │
│       • News Analyst                                                                       │
│       • Fundamentals Analyst                                                               │
│                                                                                            │
│       MODEL ROUTING: Groq Llama 3.1 70B                                                    │
│                                                                                            │
│       TOKEN BUDGET PER CALL: ≤ 700 tokens                                                  │
│                                                                                            │
│       OUTPUT: analyst_reports[ticker]                                                      │
│                                                                                            │
│   12.2 RESEARCH AGENTS — FOUR‑ROUND DEBATE (Groq Llama 3.1 70B)                            │
│       • Round 1: Bear Advocate                                                             │
│       • Round 2: Bull Advocate                                                             │
│       • Round 3: Bear Advocate                                                             │
│       • Round 4: Bull Advocate                                                             │
│                                                                                            │
│       MODEL ROUTING: Groq Llama 3.1 70B                                                    │
│                                                                                            │
│       TOKEN BUDGET PER CALL: ≤ 900 tokens                                                  │
│                                                                                            │
│       OUTPUT: debate_rounds[ticker]                                                        │
│                                                                                            │
│   12.3 RESEARCH EVALUATOR (Nvidia Nemotron 3 70B — OPENROUTER)                             │
│       • Synthesizes analyst reports + debate rounds                                        │
│                                                                                            │
│       MODEL ROUTING: Nvidia Nemotron 3 70B (OpenRouter)                                    │
│                                                                                            │
│       OPENROUTER USAGE: 1 of only 2 allowed calls per ticker                               │
│                                                                                            │
│       TOKEN BUDGET PER CALL: ≤ 1,500 tokens                                                │
│                                                                                            │
│       OUTPUT: research_view[ticker]                                                        │
│                                                                                            │
│   12.4 TRADER (Groq Llama 3.1 70B — FREE‑TIER SAFE)                                        │
│       Reads research_view, sentiment, signals, ticker_state                                │
│                                                                                            │
│       MODEL ROUTING: Groq Llama 3.1 70B                                                    │
│                                                                                            │
│       TOKEN BUDGET PER CALL: ≤ 700 tokens                                                  │
│                                                                                            │
│       OUTPUT: trader_view[ticker]                                                          │
│                                                                                            │
│   12.5 RISK ANALYST (Nvidia Nemotron 3 70B — OPENROUTER)                                   │
│       Reads trader_view, research_view, portfolio_snapshot                                 │
│                                                                                            │
│       MODEL ROUTING: Nvidia Nemotron 3 70B (OpenRouter)                                    │
│                                                                                            │
│       OPENROUTER USAGE: 2nd of only 2 allowed calls per ticker                             │
│                                                                                            │
│       TOKEN BUDGET PER CALL: ≤ 1,200 tokens                                                │
│                                                                                            │
│       OUTPUT: risk_view[ticker]                                                            │
│                                                                                            │
│   12.6 PORTFOLIO MANAGER (Groq Llama 3.1 70B — FREE‑TIER SAFE)                             │
│       FINAL internal agent.                                                                │
│                                                                                            │
│       MODEL ROUTING: Groq Llama 3.1 70B                                                    │
│                                                                                            │
│       TOKEN BUDGET PER CALL: ≤ 800 tokens                                                  │
│                                                                                            │
│       REQUIRED OUTPUT SCHEMA:                                                              │
│       {                                                                                    │
│         "action": "STRONG_BUY | BUY | HOLD | WATCH | SELL | AVOID",                        │
│         "sizing_intent": "increase | decrease | maintain",                                 │
│         "watchlist_action": "add | remove | none",                                         │
│         "rotation_flag": true/false,                                                       │
│         "strong_override": true/false,                                                     │
│         "risk_notes": "...",                                                               │
│         "confidence": 0.0-1.0,                                                             │
│         "reasoning": "..."                                                                 │
│       }                                                                                    │
│                                                                                            │
│       FINAL OUTPUT OF tradingagents.run(): pm_final_decision[ticker]                       │
└──────────────────────────────────────────────────────────────────────────────────────────┘



┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 13. GLOBAL RECONCILIATION LAYER (WHAT TO TRADE)                                           │
│                                                                                            │
│   PURPOSE: Convert per‑ticker LLM decisions into a portfolio‑level trade plan.             │
│                                                                                            │
│   INPUTS:                                                                                  │
│       • pm_final_decision[ticker]                                                          │
│       • current holdings                                                                   │
│       • portfolio constraints                                                              │
│       • watchlist state                                                                    │
│       • discovery memory                                                                   │
│       • strong overrides                                                                   │
│                                                                                            │
│   13.1 PRIORITIZATION RULES                                                                │
│       1. Forced sells                                                                      │
│       2. Strong buys                                                                       │
│       3. Regular buys                                                                      │
│       4. Watchlist actions                                                                 │
│       5. Avoid / ignore                                                                    │
│                                                                                            │
│   13.2 HOLDINGS CONSTRAINTS                                                                │
│       • Max 10 holdings                                                                    │
│       • If full → no new buys                                                              │
│       • Only sells or rotations allowed                                                    │
│                                                                                            │
│   13.3 SECTOR / COUNTRY CONSTRAINTS                                                        │
│       • If violated: downgrade buy → watch                                                 │
│       • Add penalty to discovery memory                                                    │
│                                                                                            │
│   13.4 TURNOVER LIMITS                                                                     │
│       • Prioritize sells                                                                   │
│       • Then strong buys                                                                   │
│       • Defer low‑conviction buys                                                          │
│                                                                                            │
│   13.5 ROTATION RULES                                                                      │
│       • If rotation_flag = true:                                                           │
│           - Sell weakest holding                                                           │
│           - Replace with highest‑conviction buy                                            │
│                                                                                            │
│   13.6 WATCHLIST ACTIONS                                                                   │
│       • Add if action = WATCH or conviction 0.60–0.69                                      │
│       • Remove if:                                                                          │
│           - conviction < 0.55 for 2 days                                                   │
│           - stagnation > 14 days                                                           │
│           - strong negative catalyst                                                       │
│           - promoted to holding                                                            │
│       • Manual removal: penalty_score += 1, cooldown 7–14 days                             │
│                                                                                            │
│   13.7 OUTPUT: trade_plan[ticker] (qualitative actions only)                               │
└──────────────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 14. PORTFOLIO OPTIMIZER (HOW MUCH TO TRADE)                                               │
│                                                                                            │
│   PURPOSE: Convert qualitative trade_plan into numeric target weights.                     │
│                                                                                            │
│   INPUTS:                                                                                  │
│       • trade_plan                                                                         │
│       • current weights                                                                    │
│       • covariance matrix                                                                  │
│       • portfolio constraints                                                              │
│                                                                                            │
│   14.1 ACTION → TARGET WEIGHT MAPPING                                                      │
│       • Strong buy → 6–12%                                                                 │
│       • Buy → 3–8%                                                                         │
│       • Hold → maintain                                                                    │
│       • Sell → 0%                                                                          │
│       • Avoid → 0%                                                                         │
│       • Watch → 0%                                                                         │
│                                                                                            │
│   14.2 OPTIMIZATION MODEL (PyPortfolioOpt)                                                 │
│       • Maximize risk‑adjusted return                                                      │
│       • Minimize volatility                                                                │
│       • Minimize turnover                                                                  │
│       • Constraints:                                                                       │
│           - sum(weights) = 100%                                                            │
│           - cash ≥ 5%                                                                      │
│           - max position ≤ 12%                                                             │
│           - max sector ≤ 25%                                                               │
│           - max country ≤ 40%                                                              │
│                                                                                            │
│   14.3 OUTPUT: target_weights[ticker]                                                      │
│                                                                                            │
│   14.4 SHARE QUANTITIES                                                                    │
│       • Convert weights → shares                                                           │
│       • Round to whole shares                                                              │
│       • Apply minimum trade size                                                           │
└──────────────────────────────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ 15. EXECUTION, STATE UPDATE & REASONING EXPOSURE                                          │
│                                                                                            │
│   15.1 AUTO‑EXECUTION                                                                      │
│       • If enabled: execute trades, hide recommendations                                   │
│       • If disabled: show recommendations, allow manual BUY/SELL                           │
│                                                                                            │
│   15.2 HOLDINGS UPDATE                                                                     │
│       • Update weights, entry price, timestamps, P&L                                       │
│                                                                                            │
│   15.3 WATCHLIST UPDATE                                                                    │
│       • Apply actions from Section 12                                                      │
│       • Manual removal: penalty_score += 1, cooldown 7–14 days                             │
│                                                                                            │
│   15.4 DISCOVERY MEMORY UPDATE                                                             │
│       • Update last_seen_date, penalty_score, cooldown_days                                │
│                                                                                            │
│   15.5 STRONG OVERRIDES UPDATE                                                             │
│       • Add if conviction ≥ 0.70                                                           │
│       • Remove if conviction < 0.70                                                        │
│                                                                                            │
│   15.6 UI ACTION SUMMARY (WITH REASONING)                                                  │
│       • Actions Today                                                                      │
│           - Added to watchlist → [Why?]                                                    │
│           - Removed from watchlist → [Why?]                                                │
│                                                                                            │
│       • Recommendations (if auto‑exec OFF)                                                 │
│           - Nokia — BUY — 100 — [BUY] — [Why?]                                             │
│                                                                                            │
│       • Trades Today                                                                       │
│           - Nokia — BUY — 100 executed — [Why?]                                            │
│                                                                                            │
│       WHY? BUTTON SHOWS:                                                                   │
│           pm_final_decision[ticker].reasoning                                              │
│           pm_final_decision[ticker].risk_notes                                             │
│                                                                                            │
│   15.7 TELEGRAM NOTIFICATIONS                                                              │
│       • Morning brief                                                                      │
│       • Executed trades                                                                    │
│       • Recommendations (if auto‑exec OFF)                                                 │
│                                                                                            │
│   15.8 PERSIST STATE                                                                       │
│       • holdings, watchlist, discovery memory, overrides                                   │
│       • trade history, penalties, cooldowns, daily logs                                    │
└──────────────────────────────────────────────────────────────────────────────────────────┘

