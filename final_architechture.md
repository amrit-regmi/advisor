AI FINANCIAL ADVISOR — FULL ARCHITECTURE, DATA-FLOW, IMPROVEMENT & VERIFICATION SPEC
Version: ARCHITECTURE_FULL_v5
Audience: Developer + AI Agent (build, cross-check, self-validate)

CRITICAL META-RULE FOR THE AGENT
--------------------------------
BEFORE IMPLEMENTING OR MODIFYING ANYTHING, YOU MUST:

1) VERIFY WHAT ALREADY EXISTS
   - Read the codebase, DB schema, and logs.
   - Identify which components, tables, and flows are already implemented.
   - Detect partial implementations, mocks, and stubs.

2) COMPARE AGAINST THIS ARCHITECTURE FILE
   - For each section below, mark:
     • IMPLEMENTED
     • PARTIAL
     • MISSING
   - Note mismatches between spec and reality.

3) PLAN IMPLEMENTATION ORDER
   - Prioritize missing or broken pieces that block the end-to-end flow.
   - Respect dependencies:
     data sources → discovery → Qlib → tax → TradingAgents → portfolio → sanity → delivery → backtests.

4) ONLY THEN IMPLEMENT / MODIFY
   - Never blindly overwrite existing logic.
   - Always align changes with:
     • This architecture
     • Improvement plan
     • Verification suite

5) AFTER CHANGES, RE-VERIFY
   - Re-run the verification suite (Section 9).
   - Confirm that invariants still hold.
   - Log what changed and why.

============================================================
0. OVERVIEW
============================================================
Goal:
- Turn raw global data (news, macro, markets, prices, indices) into:
  • Tax-aware, MiFID-compliant BUY/SELL/WATCH actions
  • With deep reasoning (TradingAgents multi-agent debate)
  • Sized and scheduled (Portfolio Brain)
  • Sanity-checked and selectively re-verified
  • Backtested historically (Backtrader)
  • Delivered via Telegram + Dashboard

Core pipeline (conceptual):
GDELT + FINBERT + Universe + FRED + Polymarket + Prices
→ Discovery Engine
→ Qlib Scoring
→ Finnish Tax Filter
→ TradingAgents Reasoning
→ Portfolio Brain
→ Sanity & Mid-day Reverification
→ Backtrader Performance Validation
→ Telegram + Dashboard

============================================================
1. TOOLS & STACK
============================================================
Runtime:
- OS: Ubuntu 22.04 ARM64
- Python venv: /home/ubuntu/advisor/venv
- Database: PostgreSQL (localhost)
- LLM runtime: Ollama (local)
- Web: Flask (dashboard)
- Scheduler: cron

External data sources:
- GDELT GKG (global news)
- Wikipedia index pages (universe)
- yfinance (prices, sectors)
- FRED (macro indicators)
- Polymarket (prediction markets)

Internal helpers:
- from db.database import execute, query, log, can_use_alpha_vantage

LLM models (target):
- Primary reasoning model:
  • DeepSeek-R1-Distill 14B (q4_K_M) via Ollama
  • Used for TradingAgents deep debate and final decisions
- Secondary support model:
  • Qwen2.5-7B or Mistral-7B
  • Used for fast summarization, sentiment, and UI text

Quant / NLP / Backtest tools:
- FINBERT (HuggingFace: ProsusAI/finbert or yiyanghkust/finbert-tone)
- Qlib (Microsoft’s quant research platform)
- Backtrader (backtesting engine)
- ONNX Runtime (optional acceleration)
- Optional MS time-series forecasting tools (for volatility/macro forecasts)

------------------------------------------------------------
1.1 INSTALLATION (EXPLICIT COMMANDS + HIGH-LEVEL)
------------------------------------------------------------
Base Python deps (example):
- High-level: Install core Python libraries for DB, HTTP, dataframes, web.
- Commands:
  pip install psycopg2-binary pandas numpy requests flask python-dotenv

FINBERT:
- High-level: Use HuggingFace transformers to load a financial sentiment model.
- Commands:
  pip install transformers
  pip install torch --extra-index-url https://download.pytorch.org/whl/cpu

Qlib:
- High-level: Use pyqlib in offline mode with custom data handler reading from PostgreSQL.
- Commands:
  pip install pyqlib

Backtrader:
- High-level: Use Backtrader to backtest the combined signal (Discovery + Qlib + Tax + TA + Portfolio Brain).
- Commands:
  pip install backtrader

ONNX Runtime:
- High-level: Use ONNX Runtime to accelerate FINBERT, Qlib ML models, and possibly the secondary LLM.
- Commands:
  pip install onnxruntime

Optional MS forecasting tools:
- High-level: Use open-source forecasting libs (e.g., statsmodels, scikit-learn) and MS sample notebooks for volatility/macro forecasts.
- Commands (example):
  pip install statsmodels scikit-learn

ARM NOTES:
- Use CPU builds of PyTorch and ONNX Runtime.
- Avoid GPU-specific configs.
- Keep models reasonably small (FINBERT, 7B LLMs, 14B DeepSeek quantized).

------------------------------------------------------------
1.2 GLOBAL FALLBACK RULES
------------------------------------------------------------
If a tool is missing or fails at runtime:

- FINBERT missing:
  • Use secondary LLM (Qwen/Mistral) to classify sentiment from headlines/body.
  • Store sentiment fields in news_sentiment anyway.
  • Log a warning, continue pipeline.

- Qlib missing:
  • Use a simple factor scoring function from prices:
    – momentum (e.g., 1M/3M returns)
    – volatility (std dev)
    – liquidity (volume)
  • Rank candidates using this simple model.
  • Log a warning, continue pipeline.

- Backtrader missing:
  • Skip historical performance validation.
  • Do not block daily pipeline.
  • Log a warning.

- ONNX Runtime missing:
  • Use native PyTorch/LLM inference.
  • Log a warning.

- Secondary LLM missing:
  • Use primary LLM for both deep and quick tasks (slower).
  • Log a warning.

============================================================
2. HIGH-LEVEL ARCHITECTURE DIAGRAM
============================================================

                    ┌──────────────────────────────┐
                    │      EXTERNAL DATA            │
                    │ GDELT • FRED • Polymarket     │
                    │ Wikipedia • yfinance          │
                    └──────────────┬───────────────┘
                                   │
                                   ▼
        ┌────────────────────────────────────────────────────┐
        │              DATA COLLECTION LAYER                  │
        └────────────────────────────────────────────────────┘
           │ GDELT → raw_news
           │ FINBERT/LLM → news_sentiment
           │ Universe → universe
           │ FRED → macro_data
           │ Polymarket → polymarket_signals
           │ Prices → prices
           ▼
        ┌────────────────────────────────────────────────────┐
        │         DISCOVERY ENGINE + QLIB SCORING            │
        └────────────────────────────────────────────────────┘
           │ events → discovery_candidates → ranked_candidates
           │ Qlib → qlib_ranked_candidates (10–20 tickers)
           ▼
        ┌────────────────────────────────────────────────────┐
        │              FINNISH TAX FILTER                     │
        └────────────────────────────────────────────────────┘
           │ tax_filtered_candidates
           ▼
        ┌────────────────────────────────────────────────────┐
        │            TRADINGAGENTS REASONING                 │
        └────────────────────────────────────────────────────┘
           │ recommendations (BUY/SELL/HOLD/WATCH)
           ▼
        ┌────────────────────────────────────────────────────┐
        │                PORTFOLIO BRAIN                     │
        └────────────────────────────────────────────────────┘
           │ final_actions (sized, funded)
           ▼
        ┌────────────────────────────────────────────────────┐
        │       SANITY CHECK & MID-DAY REVERIFICATION        │
        └────────────────────────────────────────────────────┘
           │ possibly updated actions / alerts
           ▼
        ┌────────────────────────────────────────────────────┐
        │          BACKTRADER PERFORMANCE VALIDATION         │
        └────────────────────────────────────────────────────┘
           │ performance metrics, hit rate, PnL
           ▼
        ┌────────────────────────────────────────────────────┐
        │              DELIVERY LAYER                         │
        └────────────────────────────────────────────────────┘
           │ Telegram (07:00 + alerts)
           │ Dashboard (Flask)
           ▼

============================================================
3. DETAILED COMPONENT ARCHITECTURE & INTERACTIONS
============================================================

3.1 GDELT + FINBERT PIPELINE
----------------------------
Files (suggested):
- pipeline/gdelt_collector.py
- pipeline/sentiment_enricher.py

Inputs:
- External: GDELT GKG files (96/day)
- Model: FINBERT (HuggingFace) or fallback LLM

Outputs:
- raw_news table (optional, but recommended):
  • article_id
  • entity
  • headline
  • body
  • source
  • timestamp

- news_sentiment table:
  • entity
  • date
  • mention_count
  • mention_velocity (30-day trend)
  • tone_avg (from GDELT)
  • tone_trajectory
  • is_accelerating (bool)
  • themes, CAMEO codes
  • finbert_sentiment_score (e.g., -1..+1)
  • finbert_sentiment_label (pos/neu/neg)
  • finbert_confidence

Interactions:
- Event classifier:
  • Uses mention_velocity, tone_trajectory, finbert_sentiment_score/label to detect sentiment shifts and events.
- TradingAgents:
  • Uses news_sentiment to build news summary and sentiment reasoning.
- Sanity checks:
  • Uses news density and sentiment flips as triggers.
- UI:
  • Ticker detail pages show article list, sentiment scores, and reasoning.

Failure modes:
- GDELT missing:
  • No news-based signals; system still runs but with reduced quality.
- FINBERT missing:
  • Fallback to secondary LLM for sentiment classification.

Validation hooks:
- Latest date in news_sentiment is yesterday or today.
- finbert_sentiment fields non-null for recent days.
- At least N entities per day.

Update triggers:
- Daily at ~06:00.

3.2 UNIVERSE LOADER
-------------------
File:
- pipeline/universe_loader.py

Inputs:
- Wikipedia index pages (pandas.read_html)
- yfinance (for validation and metadata)

Outputs:
- universe table:
  • ticker
  • company_name
  • exchange
  • sector
  • country
  • asset_type (stock, ETF, etc.)
  • yfinance_symbol

Interactions:
- Event → sector mapper:
  • Maps events to real tickers using sector/country.
- Qlib:
  • Defines instruments universe.
- Tax filter:
  • Uses country, asset_type for MiFID/tax logic.

Validation:
- No hardcoded tickers.
- Multi-country, multi-sector coverage.

3.3 FRED COLLECTOR
------------------
File:
- pipeline/fred_collector.py

Inputs:
- FRED API

Outputs:
- macro_data table:
  • series_id (e.g., FEDFUNDS, CPIAUCSL, UNRATE, DGS10)
  • date
  • value

Interactions:
- Event classifier:
  • Detects macro shocks.
- TradingAgents:
  • Macro summary for reasoning.
- Portfolio Brain:
  • Sizing bias (risk-on/off).
- Sanity checks:
  • Macro surprises as triggers.
- Qlib:
  • Regime-aware factor weighting.

3.4 POLYMARKET COLLECTOR
------------------------
File:
- pipeline/polymarket_collector.py

Inputs:
- Polymarket API

Outputs:
- polymarket_signals table:
  • market_id
  • question
  • probability
  • category
  • relevant_tickers (optional mapping)
  • last_updated

Interactions:
- Event classifier:
  • Confirms macro/financial events.
- Scorer:
  • Uses probability as a weight.
- Sanity checks:
  • Probability shifts > 10% as triggers.

3.5 PRICE COLLECTOR
-------------------
File:
- pipeline/price_collector.py

Inputs:
- yfinance
- Optional: Alpha Vantage (budgeted via can_use_alpha_vantage)

Outputs:
- prices table:
  • ticker
  • date
  • open, high, low, close, volume
  • returns, volatility (optional precomputed)

Interactions:
- Discovery:
  • Momentum alignment with events.
- Qlib:
  • Factor computation, ML features.
- TradingAgents:
  • Price context, charts.
- Portfolio Brain:
  • Sizing, PnL, entry vs current.
- Backtrader:
  • Historical OHLCV for backtests.
- Sanity checks:
  • Price moves, volatility spikes.

3.6 DISCOVERY ENGINE
--------------------
Files:
- pipeline/discovery/event_classifier.py
- pipeline/discovery/event_mapper.py
- pipeline/discovery/scorer.py

3.6.1 EVENT CLASSIFIER
Inputs:
- news_sentiment (incl. FINBERT fields)
- polymarket_signals
- macro_data
- prices (optional)

Outputs:
- events table:
  • entity
  • event_type
  • confidence
  • date
  • evidence (themes, CAMEO, macro context, sentiment)

3.6.2 EVENT → SECTOR MAPPER
Inputs:
- events
- universe
- news_sentiment
- prices
- polymarket_signals

Outputs:
- discovery_candidates:
  • ticker
  • direction (BUY/SELL)
  • event_type
  • reason
  • preliminary_score

3.6.3 SCORER
Inputs:
- discovery_candidates
- news_sentiment
- polymarket_signals
- prices

Outputs:
- ranked_candidates:
  • ticker
  • direction
  • event_type
  • final_score (0–100)
  • reason

Interactions:
- Qlib:
  • Uses ranked_candidates as candidate universe.

3.7 QLIB SCORING LAYER
----------------------
Inputs:
- ranked_candidates
- prices (historical)
- universe
- macro_data (regime)

Outputs:
- qlib_ranked_candidates:
  • ticker
  • direction
  • quant_score
  • risk_score
  • liquidity_score
  • regime_adjusted_score
  • selected_flag (top 10–20)

Implementation:
- Qlib in offline mode:
  • Custom data handler reading from PostgreSQL (prices).
  • Instruments from universe.
  • Calendar from prices dates.
- Models:
  • LightGBM or linear models for ranking.
  • Optional ONNX acceleration for inference.

Fallback:
- If Qlib missing:
  • Simple factor scoring from prices:
    – momentum, volatility, liquidity.
  • Rank and select 10–20 tickers.

Interactions:
- Tax filter:
  • Only selected tickers go through tax filter.
- Sanity checks:
  • Qlib score changes used as triggers.

3.8 FINNISH TAX FILTER
----------------------
File:
- pipeline/tax_filter.py

Inputs:
- qlib_ranked_candidates
- universe

Outputs:
- tax_filtered_candidates:
  • ticker
  • direction
  • quant_score
  • event_type
  • eligible (bool)
  • ucits_alternative (if blocked)
  • dividend_withholding_pct
  • currency
  • fx_risk
  • nordnet_tradeable
  • tax_notes
  • after_tax_yield (approx)

Rules:
- Block US ETFs (MiFID II).
- Suggest UCITS alternatives where possible.
- Add tax and FX notes.

Interactions:
- TradingAgents:
  • Receives only tax-filtered candidates.
- Portfolio Brain:
  • Uses tax_notes and after_tax_yield.

3.9 TRADINGAGENTS REASONING LAYER
---------------------------------
File:
- analysis/trading_agents_wrapper.py

Inputs:
- tax_filtered_candidates (max 5/day)
- holdings
- news_sentiment (incl. FINBERT)
- macro_data
- prices

Outputs:
- recommendations table:
  • ticker
  • action (BUY/SELL/HOLD/WATCH)
  • confidence (0–1)
  • reasoning:
    – news summary
    – macro summary
    – sentiment summary
    – technical summary (if used)
    – bull case
    – bear case
    – risks
    – PM decision rationale
    – time horizon

Config:
- deep_think_llm = DeepSeek-R1-Distill 14B
- quick_think_llm = Qwen2.5-7B or Mistral-7B
- max_debate_rounds = 5
- online_tools = False
- get_news() overridden to use DB (news_sentiment)
- price/macro context pulled from DB, not web.

Interactions:
- Portfolio Brain:
  • Uses action + confidence + reasoning.
- UI:
  • Ticker detail pages show full multi-agent trace.

3.10 PORTFOLIO BRAIN
--------------------
File:
- portfolio/portfolio_brain.py

Inputs:
- recommendations
- holdings
- user_settings:
  • nordnet_cash_eur
  • monthly_investment_budget_eur
  • monthly_invested_this_month
  • max_tradingagents_per_day
- macro_data

Outputs:
- final_actions:
  • action (BUY/SELL/WATCH/HOLD)
  • ticker
  • shares
  • price_eur
  • total_cost_eur / proceeds_eur
  • funding_source (cash / from_sells / fresh_capital)
  • reasoning (TA + portfolio logic)
  • confidence
  • tax_notes

Rules:
- SELL only if ticker in holdings.
- BUY sizes respect:
  • available cash
  • monthly budget
  • risk regime (macro_data).
- WATCH if low confidence or pending triggers.

3.11 SANITY CHECK LAYER (QUANT-ONLY)
------------------------------------
Inputs:
- final_actions
- prices
- news_sentiment
- polymarket_signals
- macro_data
- qlib_ranked_candidates

Triggers:
- Before sending morning brief.
- Mid-day (for unacted recommendations).

Checks per ticker:
- Price move > ±5%.
- Volatility spike > 2× baseline.
- GDELT/FINBERT news spike.
- Polymarket probability shift > 10%.
- Macro surprise (FRED).
- Qlib score drop > threshold.

Outputs:
- If passes:
  • final_actions unchanged.
- If fails:
  • FLAGGED_FOR_REVIEW.
  • Trigger selective TradingAgents re-run for that ticker.
  • Update recommendation and final_actions.

3.12 MID-DAY REVERIFICATION (12:00)
-----------------------------------
Inputs:
- Unacted recommendations.
- Same data as sanity layer.

Logic:
- Run sanity checks only for tickers with unacted recommendations.
- If flagged:
  • Mark as NEEDS_REEVALUATION.
  • Send Telegram alert with:
    – reason
    – conceptual [Re-run TradingAgents] button.

Constraint:
- Do NOT re-run TradingAgents for all tickers.

3.13 SELECTIVE TRADINGAGENTS RE-RUN LOGIC
-----------------------------------------
Re-run TA only when:
- sanity check fails.
- user requests re-evaluation.
- major news event.
- price shock.
- sentiment flip.
- macro shock.

Never re-run for:
- normal intraday noise.
- small sentiment changes.
- minor news.

============================================================
4. BACKTRADER PERFORMANCE VALIDATION
============================================================
File:
- analysis/backtest_engine.py (suggested)

Inputs:
- prices (historical)
- recommendations (historical)
- final_actions (historical)
- holdings history

Logic:
- Build a strategy that:
  • Enters/exits positions according to historical final_actions.
  • Uses Portfolio Brain sizing rules.
- Run Backtrader backtests:
  • Equity curve.
  • Max drawdown.
  • Sharpe ratio.
  • Hit rate (BUYs that outperformed, SELLs that avoided losses).
  • Cumulative PnL.

Outputs:
- performance_history table:
  • date, portfolio_value, cash, positions.
- strategy_metrics table:
  • sharpe, max_drawdown, hit_rate, cumulative_pnl, etc.

Uses:
- UI accuracy tracking:
  • % BUYs that outperformed.
  • % SELLs that avoided losses.
  • Avg return after recommendation.
  • Hit rate.
  • Cumulative PnL.
- Improvement plan:
  • Evaluate changes to Discovery/Qlib/TA/Portfolio Brain.
- Agent self-evaluation:
  • Check if system is improving over time.

Fallback:
- If Backtrader missing:
  • Skip backtests.
  • Log warning.
  • Do not block daily pipeline.

============================================================
5. DELIVERY LAYER
============================================================

5.1 TELEGRAM BOT
----------------
File:
- alerts/telegram_bot.py

Inputs:
- final_actions
- portfolio value
- cash
- funding summary
- flags from sanity layer (NEEDS_REEVALUATION, FLAGGED_FOR_REVIEW)

Outputs:
- Morning brief (07:00):
  • Header: date, portfolio value, cash.
  • SELL section (with reasons and PnL).
  • BUY section (with funding_source).
  • WATCH section (with triggers).
  • Funding summary (cash, from sells, fresh capital).
- Mid-day alerts:
  • NEEDS_REEVALUATION notifications.
  • Major changes.

5.2 DASHBOARD
-------------
File:
- dashboard/app.py

Routes:
- GET / → portfolio overview.
- GET /history → past recommendations and actions.
- POST /trade → log completed trade, update holdings.
- GET/POST /settings → view/update user_settings.
- GET/POST /watchlist → manage watchlist.
- GET /ticker/<ticker> → ticker detail page.

Ticker detail page must show:
- News articles:
  • headline, source, timestamp, link, summary.
  • sentiment score (FINBERT).
  • sentiment reasoning.
- TradingAgents reasoning flow:
  • news analyst, macro analyst, sentiment analyst, technical analyst.
  • bull vs bear debate.
  • risk manager.
  • PM decision.
  • confidence score.
  • key risks.
  • time horizon.
- Outcome summary:
  • entry vs current price.
  • gain/loss since recommendation.
  • performance chart.
  • “how much I won” metric.

============================================================
6. UI ARCHITECTURE (IMPROVEMENT PLAN INTEGRATED)
============================================================

6.1 Ticker Detail Pages
- Triggered from:
  • Recommendation tickers.
  • Top candidates sent to TradingAgents.
  • News-sentiment tickers.
  • Discovery engine tickers.
- Must show:
  • News articles (with sentiment).
  • TradingAgents reasoning flow.
  • Outcome summary.
  • Actions: BUY / SELL / WATCH / Re-analyze / Re-run TA.

6.2 Macro Data Tab
- For each macro indicator:
  • What it measures.
  • Why it matters.
  • What current value means.
  • Impact on investments.

6.3 Discovery Engine Context
- For each discovery candidate:
  • Why it appeared.
  • Triggering signals.
  • Momentum/volatility context.
  • News density spike.
  • Macro sensitivity.
  • Sentiment shift.
  • Factor score changes.

6.4 Buy / Sell / Watch Buttons Everywhere
- Every ticker shows:
  • BUY, SELL, WATCH.
- Dynamic state:
  • If owned → SELL + “IN PORTFOLIO”.
  • If on watchlist → “ON WATCHLIST”.
  • If not owned → BUY + WATCH.

6.5 Portfolio View Enhancements
- For each position:
  • Current price.
  • Entry price.
  • Gain/loss % and €.
  • Position weight.
  • Sector weight.
  • Mini chart of position history.

6.6 Customization Features
- Auto-Commit Recommendations:
  • Toggle to automatically execute recommendations (conceptual).
  • Only after Portfolio Brain + tax filter.
  • Log every auto-action.
- Start Clean Mode:
  • Resets positions, watchlist, history, performance.
  • Must show destructive warning.
- Accuracy Tracking:
  • % of BUYs that outperformed.
  • % of SELLs that avoided losses.
  • Average return after recommendation.
  • Hit rate.
  • Cumulative PnL.

============================================================
7. MODEL ARCHITECTURE
============================================================

7.1 TradingAgents Models
- Primary:
  • DeepSeek-R1-Distill 14B (q4_K_M) via Ollama.
- Secondary:
  • Qwen2.5-7B or Mistral-7B via Ollama.

Config:
- deep_think_llm = DeepSeek.
- quick_think_llm = Qwen/Mistral.
- max_debate_rounds = 5.
- online_tools = False.
- get_news() overridden to use DB (news_sentiment).
- Price/macro context from DB, not web.

7.2 FINBERT
- Source:
  • HuggingFace: ProsusAI/finbert or yiyanghkust/finbert-tone.
- Use:
  • Article-level sentiment classification.
  • Entity-level aggregation.
- Optional:
  • Convert to ONNX for faster inference.

7.3 Qlib
- Use:
  • Factor models.
  • ML ranking.
  • Risk models.
  • Volatility and liquidity filters.
  • Regime-aware scoring using macro_data.

7.4 ONNX Runtime
- Use:
  • Accelerate FINBERT.
  • Accelerate Qlib ML models.
  • Optionally accelerate secondary LLM (if exported).

============================================================
8. DAILY PIPELINE ARCHITECTURE
============================================================

Morning canonical flow:

1) 06:00 — Data & Signals:
   - GDELT + FINBERT pipeline:
     • raw_news → news_sentiment.
   - Universe loader:
     • universe.
   - FRED collector:
     • macro_data.
   - Polymarket collector:
     • polymarket_signals.
   - Price collector:
     • prices.
   - Discovery Engine:
     • events → discovery_candidates → ranked_candidates.
   - Qlib scoring:
     • qlib_ranked_candidates (10–20 tickers).
   - Finnish Tax Filter:
     • tax_filtered_candidates.

2) Overnight / early morning — Deep Reasoning:
   - TradingAgents:
     • tax_filtered_candidates → recommendations.
   - Portfolio Brain:
     • recommendations + holdings + user_settings → final_actions.

3) Pre-07:00 — Sanity Check:
   - Sanity layer:
     • Check triggers (price, vol, news, Polymarket, macro, Qlib).
     • If flagged → selective TA re-run → updated final_actions.

4) 07:00 — Delivery:
   - Telegram morning brief:
     • final_actions + portfolio summary.

Mid-day:
- 12:00:
  • Sanity check for unacted recommendations.
  • If flagged → NEEDS_REEVALUATION + Telegram alert.

Periodic:
- Backtrader:
  • Backtests historical signals.
  • Updates performance_history and strategy_metrics.

============================================================
9. VERIFICATION SUITE (FOR SELF-VALIDATION)
============================================================

The agent must ensure tests or checks exist for:

9.1 Data Freshness
- news_sentiment, prices, macro_data, polymarket_signals updated within last 3 days.

9.2 Universe Integrity
- universe has no hardcoded tickers.
- multiple countries and sectors present.

9.3 GDELT + FINBERT Sentiment
- news_sentiment has 30-day history for key entities.
- finbert_sentiment fields populated for recent days.

9.4 Discovery Correctness
- events exist for known macro shocks.
- discovery_candidates drawn from universe, not hardcoded.

9.5 Qlib Functionality
- qlib_ranked_candidates contains 10–20 tickers daily.
- factor exposures and scores are finite and reasonable.

9.6 Tax Filter
- No US ETF appears as eligible.
- UCITS alternatives suggested where applicable.

9.7 TradingAgents Outputs
- Each recommendation has:
  • action.
  • confidence.
  • structured reasoning fields.
- Max 5 tickers/day through TA.

9.8 Portfolio Brain Rules
- SELL only for tickers in holdings.
- BUY sizes respect cash + budget.
- funding_source consistent with cash/sells/fresh capital.

9.9 Sanity Layer
- Flags only a subset of tickers.
- Triggers selective TA re-runs, not global.

9.10 Delivery
- Telegram brief format matches spec.
- Dashboard routes respond and show correct data.

9.11 Backtrader
- performance_history and strategy_metrics populated.
- Metrics (hit_rate, cumulative_pnl) reasonable and non-NaN.

============================================================
10. AGENT SELF-VALIDATION CHECKLIST (BEFORE ANY CHANGE)
============================================================

For each run where you (the agent) plan to modify or extend the system:

1) Scan codebase and DB:
   - Identify which components exist and how complete they are.

2) Map reality → this spec:
   - For each section (3.x, 4.x, 5.x, 6.x, 7.x, 8.x, 9.x):
     • IMPLEMENTED / PARTIAL / MISSING.

3) Identify gaps:
   - Which missing/partial pieces block the end-to-end flow?

4) Plan changes:
   - Implement missing components in dependency order.
   - Avoid breaking existing working pieces.
   - Respect fallbacks and logging.

5) Implement:
   - Follow the architecture and improvement plan exactly.
   - No hardcoded tickers or credentials.
   - Use log() for major steps and decisions.

6) Re-verify:
   - Run verification suite (Section 9).
   - Confirm invariants hold.

7) Document:
   - Log what changed, why, and which tests passed.

END OF ARCHITECTURE_FULL_v5
