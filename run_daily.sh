#!/bin/bash
set -e

# Prevent concurrent pipeline runs — exit immediately if already running
LOCK_FILE=/tmp/advisor_daily.lock
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "=== $(date) Pipeline already running — exiting ==="
    exit 1
fi

STAGE_FILE=/tmp/advisor_pipeline_stage.json
STAGES=(
  "collect_all:Data Collection"
  "event_classifier:Event Classifier"
  "event_mapper:Event Mapper"
  "scorer:Candidate Scorer"
  "tax_filter:Finnish Tax Filter"
  "daily_sixteen:Daily-16 Selection"
  "trading_agents:TradingAgents Analysis"
  "reconciliation:Global Reconciliation"
  "optimizer:Portfolio Optimizer"
  "holdings_monitor:Holdings Monitor"
  "portfolio_brain:Portfolio Brain"
  "sanity_check:Sanity Check"
  "performance_tracker:Performance Tracker"
)

stage() {
  local key="$1" label="$2" status="$3"
  printf '{"stage":"%s","label":"%s","status":"%s","ts":"%s","pid":%d}\n' \
    "$key" "$label" "$status" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" \
    > "$STAGE_FILE"
}

source /home/ubuntu/venv/bin/activate
cd /home/ubuntu/advisor
echo "=== $(date) Starting daily run (SKIP_UNIVERSE=${SKIP_UNIVERSE:-0}) ==="

stage "collect_all" "Data Collection" "running"
python pipeline/collect_all.py
stage "collect_all" "Data Collection" "done"

stage "event_classifier" "Event Classifier" "running"
python pipeline/discovery/event_classifier.py
stage "event_classifier" "Event Classifier" "done"

stage "event_mapper" "Event Mapper" "running"
python pipeline/discovery/event_mapper.py
stage "event_mapper" "Event Mapper" "done"

stage "scorer" "Candidate Scorer" "running"
python pipeline/discovery/scorer.py
stage "scorer" "Candidate Scorer" "done"

stage "tax_filter" "Finnish Tax Filter" "running"
python pipeline/tax_filter.py
stage "tax_filter" "Finnish Tax Filter" "done"

stage "daily_sixteen" "Daily-16 Selection" "running"
python pipeline/daily_sixteen.py
stage "daily_sixteen" "Daily-16 Selection" "done"

stage "trading_agents" "TradingAgents Analysis (Groq+OpenRouter)" "running"
python analysis/trading_agents_wrapper.py
stage "trading_agents" "TradingAgents Analysis" "done"

stage "reconciliation" "Global Reconciliation" "running"
python portfolio/reconciliation.py
stage "reconciliation" "Global Reconciliation" "done"

stage "optimizer" "Portfolio Optimizer" "running"
python portfolio/optimizer.py
stage "optimizer" "Portfolio Optimizer" "done"

stage "holdings_monitor" "Holdings Monitor" "running"
python portfolio/holdings_monitor.py
stage "holdings_monitor" "Holdings Monitor" "done"

stage "portfolio_brain" "Portfolio Brain" "running"
python portfolio/portfolio_brain.py
stage "portfolio_brain" "Portfolio Brain" "done"

stage "sanity_check" "Sanity Check" "running"
python pipeline/sanity_check.py || true
stage "sanity_check" "Sanity Check" "done"

stage "performance_tracker" "Performance Tracker" "running"
python portfolio/performance_tracker.py || true
stage "performance_tracker" "Performance Tracker" "done"

printf '{"stage":"idle","label":"Idle","status":"idle","ts":"%s","pid":0}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STAGE_FILE"

echo "=== $(date) Daily run complete ==="
