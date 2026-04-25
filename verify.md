SYSTEM VERIFICATION SPEC (INTENT-LEVEL)

This document defines what the system must verify, why each verification matters, and what failure means. It does not include implementation-level test code. The AI agent will generate the final tests once the system stabilizes.

------------------------------------------------------------
1. Universe Integrity
------------------------------------------------------------
What:
The stock universe must be large, diverse, global, and not hardcoded.

Why:
A biased or incomplete universe destroys discovery quality.

Failure:
- Too few stocks
- Missing major countries
- Missing sectors
- Watchlist nearly equal to universe
- Hardcoded tickers anywhere in the pipeline

------------------------------------------------------------
2. GDELT Global Scanning
------------------------------------------------------------
What:
GDELT ingestion must capture global news, not only watchlist tickers.

Why:
The advisor must detect world events, not just ticker-specific headlines.

Failure:
- Only watchlist tickers appear
- No global topic coverage
- No multi-day sentiment history

------------------------------------------------------------
3. Discovery Engine Uses Universe (Not Hardcoded Tickers)
------------------------------------------------------------
What:
Discovery must map events → sectors → universe, not → fixed tickers.

Why:
Hardcoded tickers break the entire discovery pipeline.

Failure:
- Hardcoded tickers in code
- Discovery returns only FAANG
- No universe queries
- No sector-based mapping

------------------------------------------------------------
4. GDELT Trend Analysis (30-Day History)
------------------------------------------------------------
What:
Sentiment must include velocity, trend, and multi-day context.

Why:
Point-in-time sentiment is meaningless without trend.

Failure:
- Only today’s sentiment exists
- No velocity/trend fields
- No multi-day history

------------------------------------------------------------
5. Polymarket Filtering & Ticker Mapping
------------------------------------------------------------
What:
Polymarket ingestion must filter out sports/weather and map financial markets to tickers.

Why:
Sports markets pollute signals; financial markets are essential.

Failure:
- Sports/weather markets appear
- No financial markets
- No ticker mapping
- No probability extraction

------------------------------------------------------------
6. Finnish Tax Filter
------------------------------------------------------------
What:
The tax filter must block US ETFs, flag US withholding tax, and suggest UCITS alternatives.

Why:
MiFID II and Finnish tax rules require this.

Failure:
- SPY/QQQ allowed
- No UCITS alternatives
- No 15% WHT flag for US stocks
- EU stocks incorrectly flagged

------------------------------------------------------------
7. Full Pipeline Produces Valid Recommendations
------------------------------------------------------------
What:
The entire pipeline must run end-to-end and produce BUY/SELL/WATCH with reasoning.

Why:
This is the core product.

Failure:
- No recommendations
- Missing reasoning
- Invalid confidence scores
- SELL for non-held stocks
- Empty or incomplete outputs

------------------------------------------------------------
8. Telegram Brief Format
------------------------------------------------------------
What:
Telegram brief must include required sections, emojis, and formatting.

Why:
This is the user-facing output.

Failure:
- Missing sections
- Missing emojis
- Message fails to send
- Incorrect formatting

------------------------------------------------------------
9. API Budget Protection
------------------------------------------------------------
What:
Alpha Vantage usage must be capped and enforced.

Why:
Avoid API lockouts and rate-limit failures.

Failure:
- Budget not checked
- Collectors ignore limits
- System exceeds daily quota

------------------------------------------------------------
10. TradingAgents Reasoning Completeness
------------------------------------------------------------
What:
Every analysis must include:
- News summary
- Macro summary
- Sentiment summary
- Bull case
- Bear case
- Risks
- PM decision

Why:
Ensures the LLM didn’t hallucinate or skip steps.

Failure:
- Missing sections
- Empty reasoning
- Invalid PM decision

------------------------------------------------------------
11. Portfolio Brain Weight Sanity
------------------------------------------------------------
What:
Portfolio weights must sum to approximately 1.0 and be non-negative.

Why:
Ensures valid allocation and risk control.

Failure:
- Negative weights
- Sum not close to 1.0
- Missing sizing reasons

------------------------------------------------------------
12. Macro Indicator Freshness
------------------------------------------------------------
What:
Macro indicators must be updated weekly.

Why:
Stale macro data breaks regime detection.

Failure:
- Indicators older than 7 days
- Missing macro fields

------------------------------------------------------------
13. Morning Sanity Check Logic
------------------------------------------------------------
What:
Before sending the morning brief, the system must run a quant-only sanity check.

Why:
Protects against overnight shocks.

Failure:
- No sanity flags
- No selective re-runs
- No volatility or news spike detection

------------------------------------------------------------
14. Selective TradingAgents Re-Run Logic
------------------------------------------------------------
What:
Only flagged tickers should trigger a re-run.

Why:
Avoids unnecessary compute and noise.

Failure:
- Re-running all tickers
- Not re-running flagged ones
- Missing re-run logs

------------------------------------------------------------
15. Recommendation Safety
------------------------------------------------------------
What:
The system must avoid unsafe recommendations.

Why:
Protects the user from harmful trades.

Failure:
- BUY on halted stocks
- SELL on non-held stocks
- High-volatility BUYs
- Low-liquidity BUYs

------------------------------------------------------------
16. MiFID II Suitability
------------------------------------------------------------
What:
Recommendations must match user risk profile.

Why:
Regulatory compliance.

Failure:
- Unsuitable instruments recommended
- Missing suitability checks

------------------------------------------------------------
17. Performance Tracking
------------------------------------------------------------
What:
System must track accuracy, hit rate, and PnL of recommendations.

Why:
Allows continuous improvement and drift detection.

Failure:
- No performance records
- Missing returns
- Missing timestamps

------------------------------------------------------------
18. Data Freshness Across All Collectors
------------------------------------------------------------
What:
All collectors (prices, GDELT, Polymarket, macro) must produce fresh data.

Why:
Stale data leads to incorrect recommendations.

Failure:
- Any collector older than 3 days
- Missing data rows
- Partial ingestion

------------------------------------------------------------
19. Full Pipeline Integrity
------------------------------------------------------------
What:
Universe → Discovery → TradingAgents → Portfolio Brain must all produce outputs.

Why:
Ensures the system works end-to-end.

Failure:
- Any stage produces zero output
- Pipeline breaks silently
- Missing intermediate tables

------------------------------------------------------------
END OF FILE
