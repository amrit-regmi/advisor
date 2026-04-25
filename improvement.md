1. UI IMPROVEMENTS (GLOBAL)
1.1 Ticker Detail Pages (Everywhere in System)
Whenever a user clicks on:

Recommendation tickers

Top candidates sent to TradingAgents

News‑sentiment tickers

Discovery engine tickers

The system must open a dedicated ticker detail page containing:

A. News Articles
Full list of articles

Headline

Source

Timestamp

Link to original

Summary

Human‑readable sentiment score

Sentiment reasoning

Actions Buy / Sell / Watch / Re analyze / Re run Trading agent

B. TradingAgents Reasoning Flow
Show the full multi‑agent trace:

News analyst

Macro analyst

Sentiment analyst

Technical analyst

Bull vs Bear debate

Risk manager

PM decision

Final BUY/SELL/HOLD

Confidence score

Key risks

Time horizon

C. Outcome Summary
Gain/loss since recommendation

Entry vs current price

Performance chart

“How much I won” metric

1.2 Data Tab Improvements (Macro Indicators)
Each macro indicator must include:

What it measures

Why it matters

What the current value means

Impact on investments

(Use the human‑readable macro descriptions provided earlier.)

1.3 Discovery Engine Context
For each discovery candidate, show:

Why it appeared

Triggering signals

Momentum/volatility context

News density spike

Macro sensitivity

Sentiment shift

Factor score changes

1.4 Buy / Sell / Watch Buttons Everywhere
Every ticker must show:

BUY

SELL

WATCH

Dynamic state:

If owned → show SELL + “IN PORTFOLIO”

If on watchlist → show “ON WATCHLIST”

If not owned → show BUY + WATCH

1.5 Portfolio View Enhancements
For each position:

Current price

Entry price

Gain/loss %

Gain/loss €

Position weight

Sector weight

Mini chart of position history

1.6 Customization Features
Auto‑Commit Recommendations
Toggle to automatically execute recommendations

Only after portfolio brain + tax filter

Log every auto‑action

Start Clean Mode
Resets:

positions

watchlist

history

performance

Must show a destructive warning

Accuracy Tracking (Optional)
% of BUYs that outperformed

% of SELLs that avoided losses

Average return after recommendation

Hit rate

Cumulative PnL

2. MODEL RECOMMENDATIONS
2.1 Primary Reasoning Model (TradingAgents)
Code
DeepSeek-R1-Distill 14B (q4_K_M)
Best chain‑of‑thought

Best debate quality

Best risk reasoning

Fits in 24GB RAM

Slow but perfect for overnight runs

2.2 Secondary Support Model (News/Sentiment)
Code
Qwen2.5-7B or Mistral-7B
Fast

Good for summarization

Good for sentiment scoring

3. RECOMMENDATION FLOW ENHANCEMENTS
3.1 Morning Recommendation Flow (Primary)
This is the canonical daily cycle:

Run collectors (GDELT, FRED, Polymarket, prices)

Run Discovery Engine

Run Qlib scoring

Select 10–20 tickers

Run TradingAgents full analysis overnight

Run Portfolio Brain

Deliver BUY/SELL/WATCH brief at 07:00

4. SANITY CHECK LAYER (QUANT‑ONLY)
4.1 Before Sending Morning Brief
Run a fast sanity check for each ticker:

Triggers:

Price move > ±5%

Volatility spike > 2× baseline

GDELT news spike

Polymarket probability shift > 10%

Macro surprise

If sanity check passes:
→ Send morning brief unchanged.

If sanity check fails:
→ Mark ticker as FLAGGED_FOR_REVIEW  
→ Re‑run TradingAgents only for that ticker  
→ Update recommendation before sending brief

5. MID‑DAY RE‑VERIFICATION (ONLY FOR UNACTED RECOMMENDATIONS)
At 12:00:

Run sanity check again

Only for tickers with unacted recommendations

If flagged:

Mark as NEEDS_REEVALUATION

Send Telegram alert

Provide button:
[Re‑run TradingAgents for this ticker]

Do NOT re-run TradingAgents for all tickers.

6. SELECTIVE TRADINGAGENTS RE‑RUN LOGIC
Re-run TradingAgents only when:

sanity check fails

user requests re-evaluation

major news event

price shock

sentiment flip

macro shock

Never re-run for:

normal intraday noise

small sentiment changes

minor news
