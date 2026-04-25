# AI Financial Advisor

A personal AI-driven investment advisor that runs entirely on free-tier APIs. It discovers global investment opportunities by mapping news events to equities, debates each candidate through a 13-agent LLM pipeline, and delivers actionable buy/sell/watch recommendations via Telegram every morning.

Built for Finnish investors: handles MiFID II compliance, UCITS-only ETF filtering, and withholding tax metadata automatically.

> **Disclaimer:** This is a personal research project. It does not constitute financial advice. Always do your own due diligence before investing.

---

## What it does

| Step | What happens |
|------|-------------|
| Discover | Maps global news (GDELT) and prediction markets (Polymarket) to equity tickers |
| Analyse | FinBERT sentiment scoring on news headlines, multi-factor conviction score |
| Debate | 13-agent LLM pipeline: 2 analysts → 4-round Bull/Bear → Evaluator → Trader → 3-way Risk Debate → Risk Manager ↔ PM 4-turn debate → PM Final Decision |
| Size | PyPortfolioOpt target weights, capped at 12% per position |
| Monitor | Re-analyses existing holdings daily for stop-loss and rotation signals |
| Report | Telegram morning brief + Flask web dashboard (port 5000) |

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| LLM — fast agents (11 calls/ticker) | Groq `llama-3.3-70b-versatile` — free tier, 100k tokens/day/key |
| LLM — deep reasoning (4 calls/ticker) | OpenRouter `:free` models (Nvidia Nemotron, GPT-4o-mini) — 50 req/day/key |
| Sentiment | [ProsusAI/FinBERT](https://huggingface.co/ProsusAI/finbert) — runs locally on CPU |
| News & events | [GDELT GKG](https://www.gdeltproject.org/) — 15-minute global news updates |
| Macro data | [FRED API](https://fred.stlouisfed.org/) — VIX, yield curve, CPI, Fed funds rate |
| Prediction markets | [Polymarket API](https://polymarket.com/) |
| Prices | Yahoo Finance via [yfinance](https://github.com/ranaroussi/yfinance) |
| Database | PostgreSQL 14+ |
| Portfolio optimisation | [PyPortfolioOpt](https://github.com/robertmartin8/PyPortfolioOpt) |
| Backtesting | [Backtrader](https://github.com/mementum/backtrader) |
| Web dashboard | Flask 3, Bootstrap 5.3 dark theme, Chart.js |
| Notifications | Telegram Bot API |

---

## The 13-agent debate pipeline

Each ticker takes ~45 seconds and uses 11 Groq calls + 4 OpenRouter calls:

```
[Groq]        ① Market Analyst      — price trend, momentum, technicals
[Groq]        ② News Analyst        — GDELT tone, FinBERT sentiment, divergence flags

[Groq]        Bear Round 1  ←→  Bull Round 1    (4-round structured debate)
[Groq]        Bear Round 2  ←→  Bull Round 2

[Groq]        ③ Research Evaluator  — synthesises debate → Verdict + confidence score
[Groq]        ④ Trader              — preliminary BUY / SELL / HOLD / WATCH

[Groq]        ⑤ Risk Debate (3-way) — Aggressive vs Conservative vs Neutral analysts
                                       all running on Groq simultaneously

[OpenRouter]  ⑥ Risk Manager        — synthesises 3-way debate, issues 2 numbered challenges
[OpenRouter]  ⑦ PM Response         — Portfolio Manager counters each challenge with evidence
[OpenRouter]  ⑧ RM Rebuttal         — Risk Manager holds firm or concedes, states final Verdict
[OpenRouter]  ⑨ PM Final Decision   — arbitrates full debate → JSON: action / confidence / reasoning
```

The full debate trace is stored per-ticker and displayed as expandable agent cards in the dashboard.

---

## API budget

With 2 free-tier keys each:

| Provider | Daily limit | Per-ticker cost | Capacity |
|----------|------------|-----------------|----------|
| Groq | 200,000 tokens | ~5,200 tokens | ~38 tickers |
| OpenRouter | 100 requests | 4 requests | 22 scheduled + 2 on-demand |

The pipeline analyses 20 tickers/day by default: ~104k Groq tokens and 80 OR requests — within both limits.

---

## Setup

### Prerequisites

- Ubuntu 22.04+, Python 3.10+, PostgreSQL 14+
- Groq API key (free): https://console.groq.com
- OpenRouter API key (free): https://openrouter.ai
- Optional: FRED API key, Alpha Vantage key, Telegram bot token

### Install

```bash
git clone https://github.com/amrit-regmi/advisor.git
cd advisor
python3 -m venv ~/venv
source ~/venv/bin/activate
pip install -r requirements.txt
```

### Download FinBERT model

```bash
python3 -c "
from transformers import pipeline
pipeline('text-classification', model='ProsusAI/finbert',
         cache_dir='models/finbert', top_k=None)
print('FinBERT downloaded.')
"
```

### Configure

```bash
cp .env.example .env
# Edit .env and fill in your API keys and DB credentials
```

### Initialise database

```bash
psql -U advisor_user -d advisor -f db/schema.sql
```

### First-run settings

```bash
python3 - <<'EOF'
import sys; sys.path.insert(0, '.')
from db.database import execute
settings = [
    ('monthly_investment_budget_eur', '500'),
    ('nordnet_cash_eur', '2000'),
    ('simulate_recommendations', 'true'),
    ('min_confidence', '0.60'),
    ('stop_loss_pct', '15'),
]
for key, val in settings:
    execute("INSERT INTO user_settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value", (key, val))
print("Settings initialised.")
EOF
```

---

## Running

### Daily pipeline (cron at 6 AM)

```bash
bash run_daily.sh

# Skip universe refresh on non-monthly days
SKIP_UNIVERSE=1 bash run_daily.sh
```

### Dashboard

```bash
python3 dashboard/app.py
# Open http://localhost:5000
```

### Crontab example

```cron
0 6  * * 1-5  cd /home/ubuntu/advisor && SKIP_UNIVERSE=1 bash run_daily.sh >> logs/daily.log 2>&1
0 12 * * 1-5  cd /home/ubuntu/advisor && bash run_midday.sh >> logs/midday.log 2>&1
0 2  1 * *    cd /home/ubuntu/advisor && bash reload_universe.sh >> logs/universe.log 2>&1
```

---

## Project structure

```
advisor/
├── analysis/
│   ├── rate_limit_manager.py     # Groq + OpenRouter budget tracking, RPM throttle
│   └── trading_agents_wrapper.py # 13-agent LLM debate pipeline
├── alerts/
│   └── telegram_bot.py           # Morning brief + intraday alerts
├── dashboard/
│   └── app.py                    # Flask web UI
├── db/
│   ├── database.py               # psycopg2 helpers: execute(), query(), log()
│   └── schema.sql                # PostgreSQL schema
├── pipeline/
│   ├── collect_all.py            # Master data collection runner
│   ├── daily_sixteen.py          # Daily-20 ticker set builder (priority selection)
│   ├── discovery/
│   │   ├── event_classifier.py   # GDELT → event type classification
│   │   ├── event_mapper.py       # Event types → affected tickers
│   │   └── scorer.py             # 0–100 candidate scoring
│   ├── finbert_sentiment.py      # ProsusAI/FinBERT local sentiment scoring
│   ├── fred_collector.py         # FRED macro data
│   ├── gdelt_collector.py        # GDELT GKG news ingestion + FinBERT scoring
│   ├── polymarket_collector.py   # Polymarket event probabilities
│   ├── price_collector.py        # Yahoo Finance OHLCV prices
│   ├── signal_engine.py          # Multi-factor conviction score [0, 1]
│   └── tax_filter.py             # Finnish MiFID II / UCITS compliance filter
├── portfolio/
│   ├── auto_executor.py          # Simulated trade execution
│   ├── holdings_monitor.py       # Daily holdings re-analysis
│   ├── optimizer.py              # PyPortfolioOpt target weights
│   ├── portfolio_brain.py        # Final brief: sizing, regime, stop-loss
│   ├── reconciliation.py         # Portfolio-level constraint enforcement
│   └── sector_utils.py           # Sector and region name normalisation
├── tests/
│   ├── test_geo_allocation.py
│   ├── test_pipeline_e2e.py
│   └── test_sector_allocation.py
├── .env.example                  # Template — copy to .env and fill in keys
├── requirements.txt
├── run_daily.sh
├── run_midday.sh
└── reload_universe.sh
```

---

## Key design decisions

**Why 11 Groq + 4 OpenRouter calls?**
Groq's free tier is fast and cheap but Llama 3.3 70B makes better risk/reward decisions with more context. The Risk Manager ↔ PM 4-turn debate uses the same OpenRouter model on both sides so neither has a model-quality advantage — only the strength of their argument matters.

**Why FinBERT in addition to GDELT tone?**
GDELT's `AvgTone` is a raw score based on word polarity. FinBERT is a BERT model fine-tuned on financial text and better understands domain-specific language ("beats estimates", "guidance cut", "short squeeze"). When FinBERT confidence is below 0.25 the system falls back to raw GDELT tone rather than amplifying a weak signal.

**Why half-Kelly position sizing?**
Full Kelly is theoretically optimal but assumes perfect probability estimates. Halving it absorbs model error and liquidity constraints without sacrificing much expected return.

---

## Credits and attributions

This project draws on, or was inspired by, the following open-source work:

| Project | Use | License |
|---------|-----|---------|
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | Multi-agent debate architecture pattern — the agent role design (analysts → debate → evaluator → trader → risk → PM) is directly inspired by this library. The debate loop and role separation in `trading_agents_wrapper.py` follow the same philosophy. | Apache 2.0 |
| [ProsusAI/finbert](https://huggingface.co/ProsusAI/finbert) | FinBERT model for financial sentiment classification (positive / negative / neutral). Used locally via `transformers.pipeline`. | CC BY 4.0 |
| [robertmartin8/PyPortfolioOpt](https://github.com/robertmartin8/PyPortfolioOpt) | Portfolio optimisation: efficient frontier, target weights, constraint enforcement. | MIT |
| [ranaroussi/yfinance](https://github.com/ranaroussi/yfinance) | Yahoo Finance wrapper for historical OHLCV prices and fundamentals. | Apache 2.0 |
| [mementum/backtrader](https://github.com/mementum/backtrader) | Historical strategy backtesting engine. | GPL 3.0 |
| [GDELT Project](https://www.gdeltproject.org/) | Free real-time global news database. Used for event detection, ticker mention velocity, and tone scoring. | Open / public domain |
| [FRED (Federal Reserve Bank of St. Louis)](https://fred.stlouisfed.org/) | Macro economic data (VIX, yield curve, CPI, Fed funds rate, credit spreads). | Public domain |
| [Polymarket](https://polymarket.com/) | Prediction market probabilities for macro and company events. | — |

### TradingAgents — extended note

The multi-agent debate structure in this project is directly inspired by **TauricResearch/TradingAgents**. That library introduced the idea of having specialised LLM agents — market analyst, fundamentals analyst, bear researcher, bull researcher, risk analyst, portfolio manager — debate a trade before making a decision.

This codebase does not use TradingAgents as a library dependency. Instead, it re-implements the debate pattern from scratch in `analysis/trading_agents_wrapper.py` to work within free-tier API constraints (Groq token limits, OpenRouter request limits) and to add a 3-way Groq risk debate + 4-turn Risk Manager ↔ Portfolio Manager arbitration round. If you are starting a new project, the TauricResearch library is worth evaluating.

---

## License

MIT — see [LICENSE](LICENSE).

You are free to use, modify, and distribute this code. If you build something interesting with it, a mention is appreciated but not required.
