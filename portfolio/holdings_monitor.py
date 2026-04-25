"""
Holdings monitor — runs TradingAgents analysis on current holdings.
"""
import sys
sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import query, log
from analysis.trading_agents_wrapper import analyze_holdings


def run():
    log('INFO', 'holdings_monitor', 'Starting holdings analysis...')
    recs = analyze_holdings()
    log('INFO', 'holdings_monitor',
        f'Holdings analysis complete: {len(recs)} recommendations')
    return recs


if __name__ == '__main__':
    recs = run()
    for r in recs:
        print(f"{r['action']} {r['ticker']} (conf={r['confidence']:.2f}): "
              f"{r['reasoning'][:80]}")
