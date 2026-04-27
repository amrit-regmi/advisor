# Candidate Discovery & Prioritisation Architecture

How a ticker goes from the global universe to the agent debate table.

---

## End-to-end flow

```
UNIVERSE (~1 k validated tickers)
        │
        ▼
DATA COLLECTION  ──────────────────────────────────────────────────────────
  prices (yfinance, daily)  │  GDELT sentiment (every 15 min)
  FRED macro (daily)        │  Polymarket signals (every 6 h)
        │
        ▼
DISCOVERY ENGINE  (runs once per day, 04:00)
  1. Event Classifier   → detects GDELT/Polymarket macro events
  2. Event Mapper       → maps events to affected sectors → candidate rows
  3. Candidate Scorer   → scores each candidate 0–100
  4. Finnish Tax Filter → blocks US ETFs, adds WHT / FX metadata
        │
        ▼  discovery_candidates table  (eligible = TRUE rows)
        │
SIGNAL ENGINE  (called per-ticker on demand)
  compute_conviction(ticker) → float [0, 1]
        │
        ▼
DAILY SELECTION  (assembles the analysis set, target = DAILY_ANALYSIS_COUNT)
  Priority 1 – Holdings (all current positions)
  Priority 2 – Watchlist (top 3 by conviction)
  Priority 3 – Strong overrides (conviction ≥ 0.70, up to 3)
  Priority 4 – Discovery candidates (up to 2, portfolio has room)
  Priority 5 – Rotation fill (remaining slots, signal-ranked + DRP)
        │
        ▼  daily_selection list  [{ticker, state, conviction}]
        │
CONTEXT BUILDER  (per-ticker, called by trading_agents_wrapper)
  signals · conviction · sentiment · discovery_meta · portfolio_snapshot
        │
        ▼
AGENT DEBATE  (trading_agents_wrapper.py)
  Market Analyst → News Analyst → Bear×2 / Bull×2 → Evaluator →
  Trader → Risk Debate (×3) → Risk Manager → PM Draft → PM Final
        │
        ▼  recommendations table
        │
RECONCILIATION → OPTIMIZER → PORTFOLIO BRAIN → Telegram brief
```

---

## 1. Universe construction

**Source:** `adanos-software/free-ticker-database` (GitHub CSV, 61 k+ tickers)
**File:** `pipeline/universe_loader.py`

| Step | Detail |
|------|--------|
| Download | Monthly refresh; cached at `data/universe_cache/tickers_raw.csv` |
| Exchange filter | Keeps only target exchanges (see table below) |
| yfinance validation | Weekly; batch of 50; marks `validated = TRUE` if price data exists in last 5 days |
| Storage | `universe` table — upsert on conflict |

**Target exchanges**

| Code | Suffix | Country | Notes |
|------|--------|---------|-------|
| NASDAQ | — | US | |
| NYSE | — | US | |
| NYSE MKT | — | US | formerly AMEX |
| NYSE ARCA | — | US | ETF-heavy; US ETFs blocked later |
| XETRA | .DE | DE | |
| LSE | .L | GB | |
| EURONEXT | .PA | FR | Paris primary |
| AMS | .AS | NL | Amsterdam |
| BME | .MC | ES | Madrid |
| HEL | .HE | FI | Helsinki |
| STO | .ST | SE | Stockholm |
| CPH | .CO | DK | Copenhagen |
| OSL | .OL | NO | Oslo |
| SIX | .SW | CH | |
| TSE | .T | JP | |
| HKEX | .HK | HK | |
| ASX | .AX | AU | |

The `UNIVERSE_MARKETS` env var / DB setting narrows this list. Empty = all exchanges.

---

## 2. Data collection

All collected by `pipeline/collect_all.py`, triggered daily at 04:00.

| Source | File | Frequency | What it feeds |
|--------|------|-----------|---------------|
| yfinance prices | `price_collector.py` | Daily (after close) | `prices` table |
| GDELT API | `gdelt_collector.py` | Every 15 min | `news_sentiment` table |
| FinBERT (local) | `finbert_sentiment.py` | After each GDELT run | `news_sentiment` — finbert columns |
| FRED | `fred_collector.py` | Daily morning | `macro_data` table |
| Polymarket | `polymarket_collector.py` | Every 6 hours | `polymarket_signals` table |
| Fundamentals | `fundamentals_cache.py` | Daily | DB cache |

**GDELT `news_sentiment` columns used downstream:**
`mention_velocity`, `avg_tone`, `tone_trajectory`, `is_accelerating`,
`finbert_sentiment_score`, `finbert_confidence`, `top_themes`

---

## 3. Discovery engine

### 3.1 Event classifier (`pipeline/discovery/event_classifier.py`)

Detects macro-level events. Runs once per day.

**Event types:** `HEALTH_CRISIS`, `GEOPOLITICAL`, `TECH_BREAKTHROUGH`, `MACRO_SHIFT`,
`ENERGY`, `SUPPLY_CHAIN`, `REGULATORY`

**Per-event scoring:**

```
gdelt_score  = theme hits (×0.65) + keyword hits (×0.35)  →  [0, 1]
               accelerating rows (velocity ≥ 1.3 or is_accelerating) weighted up to 2×
poly_score   = matched Polymarket questions × probability_signal  →  [0, 1]
               probability_signal = |prob − 0.5| × 2   (0 = neutral, 1 = extreme)

confidence   = gdelt_score × 0.60 + poly_score × 0.40

threshold    = 0.40  (MIN_CONFIDENCE)
```

Events below 0.40 confidence are dropped. Detected events are logged to `system_logs`.

### 3.2 Event mapper (`pipeline/discovery/event_mapper.py`)

Maps each detected event → buy/sell sector buckets → individual tickers from universe.

**Sector mappings (examples):**

| Event | Buy sectors | Sell sectors |
|-------|-------------|--------------|
| TECH_BREAKTHROUGH | Semiconductors, Technology, Software | Traditional Media, Retail |
| GEOPOLITICAL | Aerospace & Defense, Energy | Airlines, Tourism, Autos |
| MACRO_SHIFT | Banking, Insurance, Financials | Real Estate, Utilities, REITs |
| HEALTH_CRISIS | Healthcare, Biotech, Pharma | Airlines, Hotels, Leisure |
| ENERGY | Oil & Gas, Mining | Airlines, Chemicals, Industrials |
| SUPPLY_CHAIN | Logistics, Shipping, Industrials | Manufacturing, Consumer Discretionary |
| REGULATORY | Legal Services, Compliance | Technology, Pharmaceuticals |

Matching is fuzzy (substring, case-insensitive both ways).

**Per-ticker base score (before scorer):**

```
base_score = event_confidence
           + 0.10 if velocity > 1.5  (GDELT already accelerating)
           + 0.05 if price momentum > +2%
           + 0.10 if Polymarket probability > 60% or < 30% for event keywords
```

Saves to `discovery_candidates` table: `ticker, direction, event_type, reason, total_score`

Cap: 30 BUY candidates + 20 SELL candidates per event.

### 3.3 Candidate scorer (`pipeline/discovery/scorer.py`)

Rescores and re-ranks all today's unanalyzed candidates. Produces final `total_score` (0–100).

**Score components:**

| Component | Max points | Source |
|-----------|-----------|--------|
| GDELT velocity | 25 | `mention_velocity`: 1× = 0 pts, 2× = 12.5, 3×+ = 25 |
| GDELT tone | 25 | `avg_tone` + `tone_trajectory`; direction-aware (BUY rewards positive, SELL rewards negative) |
| Polymarket | 20 | Matched markets × `|prob − 0.5| × 2` |
| Price momentum | 20 | 7-day return vs direction; clipped at ±15% |
| Multi-source | 10 | Bonus when ≥ 2 sources confirm same direction |
| **Total** | **100** | |

**DRP applied during top-20 selection** (alpha = 10.0 on 0–100 scale):
```
threshold = 2 × sector_target_weight × 50   (N_disc = 50)
penalty   = 10.0 × max(0, sector_count − threshold)
final     = total_score − drp_penalty
```
Iterative greedy selection ensures no single sector floods the top-20 shortlist.

### 3.4 Finnish tax filter (`pipeline/tax_filter.py`)

**Blocks:** US ETFs (`asset_type = 'etf'` + US country). MiFID II / PRIIPs — not available to EU retail investors.

**Adds metadata to each candidate:**

| Field | Source |
|-------|--------|
| `eligible` | False if blocked |
| `dividend_withholding_pct` | By country (US = 15%, CH = 35%, DK = 27%, FI/DE/FR = 0%) |
| `currency` | Derived from country |
| `fx_risk` | `low` (EUR), `medium` (USD/GBP/CHF), `high` (others) |
| `nordnet_tradeable` | Exchange in known Nordnet list |
| `tax_notes` | Human-readable WHT + FX notes |

Marks `eligible = TRUE` in `discovery_candidates` for passing tickers.

---

## 4. Signal engine (`pipeline/signal_engine.py`)

Called on demand for any ticker. Returns conviction score in [0, 1].

**`compute_conviction(ticker)` components:**

| Signal | Weight | Computation |
|--------|--------|-------------|
| `momentum_7d` | +0.20 | 7-day price return mapped from [−10%, +10%] → [0, 1] |
| `sentiment_avg` | +0.25 | GDELT tone + FinBERT confidence + velocity bonus + acceleration bonus + trajectory bonus |
| `fundamentals` | +0.15 | 30-day price stability (inverse of coefficient of variation) |
| `catalyst` | +0.15 | Best discovery_candidates score from last 3 days (BUY direction) |
| `macro_alignment` | +0.10 | FRED: fed_rate < 4% → +0.05; inflation < 3% → +0.05 |
| `volatility_penalty` | −0.10 | Annualised 30-day vol / 100% |
| `multi_confirm` | +0.15 | +0.33 per confirming source (GDELT velocity > 1.5×, Polymarket > 60%, price up) |

```
raw       = sum(weight × signal)
conviction = clip(raw / 0.90, 0, 1)   # 0.90 = max positive weight sum
```

**Sentiment detail:**
```
tone_norm     = clip(0.5 + avg_tone / 20,  0, 1)
vel_bonus     = min(0.10, (velocity − 1) × 0.05)  if velocity > 1
acc_bonus     = 0.05  if any day is_accelerating in last 7 days
traj_bonus    = clip(trajectory / 20, −0.05, +0.05)

if finbert_confidence < 0.25:
    tone_weighted = tone_norm          # raw GDELT only, FinBERT too uncertain
else:
    tone_weighted = 0.5 + (tone_norm − 0.5) × finbert_confidence

sentiment_score = clip(tone_weighted + vel_bonus + acc_bonus + traj_bonus, 0, 1)
```

---

## 5. Daily selection (`pipeline/daily_selection.py`)

Assembles the ordered candidate set sent to agents. Target size = `DAILY_ANALYSIS_COUNT` (default 20).

### 5.1 Priority slots

```
SLOT               CAP      CONDITION
──────────────────────────────────────────────────────────
1. Holdings        all      active = TRUE, shares > 0
2. Watchlist       top 3    conviction-ranked; active = TRUE
3. Strong override up to 3  conviction ≥ 0.70; not in holdings/watchlist
4. Discovery       up to 2  eligible = TRUE, BUY direction; portfolio not full
5. Rotation fill   fills    remainder to TARGET_COUNT
```

**Portfolio full** (holdings ≥ MAX_HOLDINGS): slots 3 and 4 are skipped.

Rotation fill runs in `rotation_mode = True` when portfolio is full — DRP intra-sector exemption activates (see §5.3).

### 5.2 Rotation fill pool construction

Candidates for the fill slots come from a signal-ranked universe query:

```sql
SELECT ticker, sector, country,
       COALESCE(max(discovery_candidates.total_score) last 7d, 0)  AS disc_score,
       COALESCE(avg(news_sentiment.avg_tone + finbert_score) last 7d, 0)  AS sentiment_score,
       EXISTS(prices last 7d)                                           AS has_prices
FROM universe
WHERE active = TRUE AND ticker NOT IN <already_selected>
ORDER BY disc_score DESC, sentiment_score DESC, has_prices DESC, RANDOM()
LIMIT n × 6
```

Top 60 from this pool are conviction-scored via `compute_conviction()`, then re-ranked with portfolio diversity adjustments.

### 5.3 Score adjustments and DRP

Two complementary penalty/bonus systems apply before final ranking:

**A. Portfolio-level adjustments** (`adjust_score`, based on current holdings)

```
sector_utilisation = holdings_in_sector / max_sector_slots

overweight_penalty = max(0, (utilisation − 0.75) / 0.25) × 0.20   [up to −0.20]
                     SUPPRESSED in rotation_mode (intra-sector candidates must surface)

diversification_bonus = max(0, (0.50 − utilisation) / 0.50) × 0.10  [up to +0.10]

novelty_bonus = +0.05  if sector not seen in recommendations in last 7 days

Region adjustments follow the same curves at half magnitude (±0.10 / ±0.05).
```

**B. Candidate-list-level: Diminishing Returns Penalty** (`compute_drp`, based on shortlist so far)

```python
threshold = 2.0 × target_weight[sector] × N
excess    = max(0, candidate_sector_counts[sector] − threshold)
penalty   = alpha × excess
```

| Stage | N | alpha | Scale |
|-------|---|-------|-------|
| Discovery scorer (top-20 selection) | 50 | 10.0 | 0–100 |
| Discovery candidates slot | 2 | 0.10 | 0–1 |
| Rotation fill | n (slots remaining) | 0.10 | 0–1 |

**Rotation mode exemption:** if `rotation_mode = True` and the candidate's sector matches a currently-held sector, DRP = 0. This ensures MSFT → NVDA intra-sector replacements always surface. Cross-sector candidates keep the penalty.

**Combined final score:**

```
final_score = conviction
            − overweight_penalty   (portfolio-level)
            − region_penalty       (portfolio-level)
            + diversification_bonus
            + region_bonus
            + novelty_bonus
            − drp_penalty          (candidate-list-level, recomputed each pick)
```

### 5.4 Greedy iterative selection

Both the discovery slot and rotation fill use iterative greedy selection rather than sort-once-pick:

```
candidate_sector_counts = {}
remaining = pre_scored_candidates
result = []

while len(result) < target and remaining:
    for each candidate in remaining:
        drp = compute_drp(sector, candidate_sector_counts, ...)
        score = adj_score − drp
    pick = argmax(score) where sector_cap and region_cap not exceeded
    result.append(pick)
    update sector_counts, region_counts, candidate_sector_counts
```

This prevents any single sector from dominating the shortlist even when it has the highest raw conviction scores.

### 5.5 Hard caps (sector and region)

On top of the soft penalties, hard caps block tickers once a sector or region is full:

```
sector_cap  = holdings_in_sector + max(buffer, (max_slots − holdings_in_sector) × 2)
region_cap  = holdings_in_region + max(buffer, (max_slots − holdings_in_region) × 2)

# buffer = 1 normally; = round(target_count / max_holdings) in rotation_mode
```

A safety top-up runs after the main loop: if caps left unfilled slots, remaining scored tickers are added without constraints.

### 5.6 Output

`last_daily_selection` persisted to `user_settings`:
```json
{"date": "2026-04-27", "tickers": [
  {"ticker": "TSM",    "state": "holding",      "conviction": 0.72},
  {"ticker": "NVDA",   "state": "watchlist",    "conviction": 0.68},
  {"ticker": "WALD",   "state": "strong_override", "conviction": 0.71},
  {"ticker": "AELIS.PA","state": "discovery",   "conviction": 0.60},
  ...
]}
```

Discovery-sourced picks are flagged `selected_for_analysis = TRUE` in `discovery_candidates`.

---

## 6. Context builder (`pipeline/context_builder.py`)

Called once per ticker by `trading_agents_wrapper` before the agent debate. Aggregates all signals into a single dict.

```python
context[ticker] = {
    "ticker":             "NVDA",
    "ticker_state":       "watchlist",          # holding | watchlist | discovery | strong_override | rotation_fill
    "conviction":         0.68,
    "strong_override":    False,                # True when conviction ≥ 0.70
    "signals": {
        "momentum_7d":       0.72,
        "sentiment_avg":     0.61,
        "fundamentals":      0.58,
        "catalyst":          0.80,
        "macro_alignment":   0.55,
        "volatility_penalty":0.22,
        "multi_confirm":     0.66,
    },
    "sentiment": {                              # from FinBERT / GDELT
        "label": "positive",
        "confidence": 0.83,
        ...
    },
    "discovery_metadata": {
        "discovery_score":   75.0,              # 0–100
        "direction":         "BUY",
        "event_type":        "TECH_BREAKTHROUGH",
        "reason":            "...",
    },
    "watchlist_metadata": {
        "days_on_watchlist": 4,
    },
    "portfolio_snapshot": {
        "n_holdings":        8,
        "holdings":          ["TSM", "AELIS.PA", ...],
        "cash_eur":          200.0,
        "budget_left_eur":   800.0,
        "sector_exposure":   {"Technology": 3, "Healthcare": 2, ...},
        "constraints": {
            "max_holdings":          10,
            "max_sector_weight_pct": 25,
            "max_position_weight_pct":12,
            "cash_min_pct":          5,
        },
    },
}
```

Portfolio snapshot is computed once and shared across all tickers in the selection.

---

## 7. Agent debate (`analysis/trading_agents_wrapper.py`)

Each ticker gets a full 8-step debate pipeline. Rate limits are enforced by `analysis/rate_limit_manager.py`.

**Model assignments:**

| Agent | Model | Budget per call |
|-------|-------|----------------|
| Market Analyst | Groq llama-3.3-70b | ≤ 130 tokens |
| News Analyst | Groq llama-3.3-70b | ≤ 130 tokens |
| Bear Round 1 & 2 | Groq llama-3.3-70b | ≤ 150 tokens each |
| Bull Round 1 & 2 | Groq llama-3.3-70b | ≤ 150 tokens each |
| Research Evaluator | OpenRouter Nvidia Nemotron | ≤ 250 tokens |
| Trader | Groq llama-3.3-70b | ≤ 95 tokens |
| Risk Debate (×3: aggressive / conservative / neutral) | Groq llama-3.3-70b | ≤ 80 tokens each |
| Risk Manager | Groq or OpenRouter | ≤ 150 tokens |
| PM Draft | OpenRouter Nvidia Nemotron | ≤ 300 tokens |
| PM Final | Groq llama-3.3-70b | ≤ 200 tokens |

**Daily budget:**
- Groq: ≤ 95,000 tokens/day (free tier: 12k TPM, 28 RPM)
- OpenRouter: ≤ 45 requests/day (Nvidia Nemotron free tier)
- At 2 OR calls per ticker × 20 tickers = 40 OR calls/day (within budget)

**PM decision schema:**
```json
{
  "action":          "STRONG_BUY | BUY | HOLD | WATCH | SELL | AVOID",
  "sizing_intent":   "increase | decrease | maintain",
  "watchlist_action":"add | remove | none",
  "rotation_flag":   true,
  "strong_override": false,
  "risk_notes":      "...",
  "confidence":      0.70,
  "reasoning":       "..."
}
```

All intermediate outputs are stored in `debate_trace` (JSON) and persisted to `recommendations.debate_trace`.

---

## 8. Key thresholds at a glance

| Constant | Value | Where used |
|----------|-------|-----------|
| `MIN_CONFIDENCE` (event) | 0.40 | Event classifier threshold |
| `STRONG_CONVICTION_THRESHOLD` | 0.70 | Strong override slot |
| `WATCHLIST_CONVICTION_THRESHOLD` | 0.55 | Watchlist exit |
| `WATCHLIST_MAX_STAGNATION_DAYS` | 14 | Watchlist exit |
| `MAX_HOLDINGS` | 10 (configurable) | Portfolio cap |
| `MAX_SECTOR_EXPOSURE` | 25% | Sector hard cap |
| `MAX_SINGLE_STOCK` | 12% | Position weight cap |
| `DAILY_ANALYSIS_COUNT` | 20 (configurable) | Analysis set size |
| `MIN_MOMENTUM_SIGNAL` | 5.0 | Mapper momentum gate |
| Discovery slots | 2 | Per day, portfolio not full |
| Watchlist slots | 3 | Top by conviction |
| Strong override slots | 3 | Per day |
| DRP alpha (analysis) | 0.10 | 0–1 conviction scale |
| DRP alpha (discovery) | 10.0 | 0–100 score scale |
| DRP N_disc | 50 | Discovery pool size |

---

## 9. Run order (`run_daily.sh`)

```
04:00  collect_all.py          → prices, GDELT, FRED, Polymarket, fundamentals
       event_classifier.py     → detect macro events
       event_mapper.py         → map events → discovery_candidates
       scorer.py               → score candidates 0–100 (with DRP)
       tax_filter.py           → mark eligible=TRUE, add tax metadata
       daily_selection.py      → assemble analysis set (priority slots + DRP)
       trading_agents_wrapper  → analyze_daily_selection() → agent debates
       reconciliation.py       → per-ticker decisions → trade_plan
       optimizer.py            → trade_plan → target weights
       holdings_monitor.py     → check existing holdings for stop-loss triggers
       portfolio_brain.py      → final recs + auto-execute if enabled
       sanity_check.py         → validation
       performance_tracker.py  → log P&L

07:00  send_brief.sh           → Telegram morning brief
12:00  run_midday.sh           → intraday holdings monitor
```
