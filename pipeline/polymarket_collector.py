import requests
import sys
import json
from datetime import date
sys.path.insert(0, '/home/ubuntu/advisor')
from db.database import execute, log

POLYMARKET_API = "https://gamma-api.polymarket.com/markets"
RELEVANT_KEYWORDS = [
    'fed', 'federal reserve', 'interest rate', 'inflation',
    'recession', 'gdp', 'unemployment', 'oil', 'crude',
    'ukraine', 'russia', 'china', 'taiwan', 'war', 'sanctions',
    'election', 'trump', 'tariff', 'trade war', 'congress',
    'nvidia', 'apple', 'microsoft', 'tsmc', 'tesla', 'amazon',
    'bitcoin', 'crypto', 'nasdaq', 'sp500', 's&p',
    'semiconductor', 'artificial intelligence',
    'earnings', 'quarterly', 'revenue', 'stock',
    'ipo', 'merger', 'acquisition', 'bankruptcy',
    'ecb', 'bank of england', 'rate cut', 'rate hike',
    'euro', 'dollar', 'yen', 'currency',
    'imax', 'netflix', 'google', 'meta', 'alphabet'
]

EXCLUDE_KEYWORDS = [
    # Sports
    'soccer', 'football', 'basketball', 'baseball', 'hockey',
    'tennis', 'golf', 'esports', 'gaming', 'game winner',
    'goalscorer', 'anytime scorer', 'map handicap',
    'fc vs', 'fk vs', 'sk vs', 'la liga', 'premier league',
    'champions league', 'world cup', 'nba', 'nfl', 'nhl', 'mlb',
    'kills over', 'kills under', 'o/u 1.5', 'o/u 2.5',
    'o/u 3.5', 'o/u 4.5', 'handicap',
    # Weather / environment
    'temperature', 'weather', 'celsius', 'fahrenheit',
    # Pure politics — no financial relevance
    'mayoral', 'mayor of', 'gubernatorial', 'governor of',
    'senate seat', 'house seat', 'congressional seat', 'parliament seat',
    'parliamentary election', 'primary election', 'wins the election',
    'most seats', 'win the most votes', 'most votes in',
    'leave the cabinet', 'resign from', 'cabinet before',
    # Entertainment / awards
    'best director', 'best actor', 'best picture', 'oscar', 'grammy',
    'emmy', 'anime', 'manga', 'film festival',
    # Social media metrics
    'tweets in', 'posts in', 'followers',
]

TICKER_KEYWORDS = {
    'NVDA': ['nvidia', 'ai chip', 'gpu market'],
    'AAPL': ['apple stock', 'apple inc', 'iphone sales', 'apple earnings', 'aapl'],
    'TSMC': ['tsmc', 'taiwan semiconductor'],
    'XOM': ['exxon', 'xom stock'],
    'MSFT': ['microsoft stock', 'azure', 'msft'],
    'TSLA': ['tesla stock', 'tesla earnings', 'tsla'],
    'AMZN': ['amazon stock', 'amazon earnings', 'aws revenue', 'amzn'],
    'GOOGL': ['google stock', 'alphabet earnings', 'googl'],
    'META': ['meta stock', 'meta earnings', 'facebook stock'],
    'IMAX': ['imax stock', 'imax earnings'],
}

def fetch_markets():
    try:
        params = {
            'limit': 100,
            'active': 'true',
            'closed': 'false',
            'order': 'volume',
            'ascending': 'false'
        }
        response = requests.get(POLYMARKET_API, params=params, timeout=30)
        if response.status_code != 200:
            log('ERROR', 'polymarket', f'Bad response: {response.status_code}')
            return []
        return response.json()
    except Exception as e:
        log('ERROR', 'polymarket', f'Error fetching markets: {e}')
        return []

def get_probability(market):
    try:
        outcomes = market.get('outcomePrices', '[]')
        if isinstance(outcomes, str):
            prices = json.loads(outcomes)
        else:
            prices = outcomes
        if prices and len(prices) > 0:
            return float(prices[0])
    except:
        pass
    return 0.5

SECTOR_KEYWORDS = {
    'Healthcare':           ['fda', 'vaccine', 'pandemic', 'drug', 'pharma', 'biotech', 'disease'],
    'Technology':           ['ai', 'artificial intelligence', 'semiconductor', 'chip', 'software', 'cloud'],
    'Energy':               ['oil', 'crude', 'opec', 'natural gas', 'pipeline', 'energy', 'lng'],
    'Financials':           ['fed', 'federal reserve', 'interest rate', 'inflation', 'bank', 'credit'],
    'Aerospace & Defense':  ['military', 'war', 'sanctions', 'defense', 'nato', 'conflict'],
    'Consumer Discretionary': ['tariff', 'trade', 'consumer', 'retail', 'amazon', 'tesla'],
    'Real Estate':          ['housing', 'mortgage', 'reit', 'property'],
    'Utilities':            ['electricity', 'grid', 'nuclear', 'power'],
    'Materials':            ['copper', 'gold', 'mining', 'commodity', 'inflation'],
}


def find_relevant_tickers(question):
    question_lower = question.lower()
    relevant = []
    for ticker, keywords in TICKER_KEYWORDS.items():
        if any(kw in question_lower for kw in keywords):
            relevant.append(ticker)
    return relevant


def find_relevant_sectors(question):
    question_lower = question.lower()
    matched = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        if any(kw in question_lower for kw in keywords):
            matched.append(sector)
    return matched

FINANCIAL_REQUIRED = [
    # Stock / market structure
    'stock', 'share price', 'market cap', 'earnings', 'revenue', 'ipo',
    'merger', 'acquisition', 'dividend', 'nasdaq', 's&p 500', 's&p500',
    'dow jones', 'ftse', 'dax', 'nikkei', 'hang seng',
    # Macro / rates
    'gdp', 'inflation', 'interest rate', 'rate cut', 'rate hike',
    'fed funds', 'federal reserve', 'ecb', 'central bank', 'recession',
    'yield curve', 'treasury',
    # Commodities / crypto / FX
    'oil price', 'crude oil', 'opec', 'natural gas', 'gold price',
    'bitcoin', 'ethereum', 'crypto', 'usd/eur', 'eur/usd',
    # Trade / policy with financial impact
    'tariff', 'trade war', 'trade deal', 'sanctions on', 'export ban',
    'semiconductor', 'chip shortage', 'ai company', 'ai stock',
    # Specific company tickers / names with financial context
    'nvidia', 'apple stock', 'microsoft stock', 'amazon stock',
    'google stock', 'alphabet', 'meta stock', 'tesla stock',
    'tsmc', 'imax', 'netflix stock',
    # Price level questions (highly specific financial)
    'above $', 'below $', 'close at', 'close above', 'close below',
    'finish above $', 'finish below $',
]

def is_relevant(market):
    question = market.get('question', '').lower()
    description = market.get('description', '').lower()
    text = question + ' ' + description

    if any(ex in text for ex in EXCLUDE_KEYWORDS):
        return False

    # Must have a direct ticker match OR financial keyword
    has_ticker = bool(find_relevant_tickers(question))
    has_finance = any(kw in text for kw in FINANCIAL_REQUIRED)
    return has_ticker or has_finance
def run():
    log('INFO', 'polymarket', 'Fetching Polymarket markets...')
    markets = fetch_markets()

    if not markets:
        log('WARNING', 'polymarket', 'No markets returned')
        return

    relevant = [m for m in markets if is_relevant(m)]
    log('INFO', 'polymarket', f'Found {len(relevant)} relevant markets out of {len(markets)}')

    saved = 0
    for market in relevant:
        question = market.get('question', '')
        market_id = market.get('id', '')
        probability = get_probability(market)
        relevant_tickers = find_relevant_tickers(question)
        relevant_sectors = find_relevant_sectors(question)

        execute("""
            INSERT INTO polymarket_signals
                (market_id, question, probability, date,
                 relevant_tickers, relevant_sectors)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            market_id,
            question,
            probability,
            date.today(),
            ','.join(relevant_tickers),
            ','.join(relevant_sectors),
        ))
        saved += 1

        if relevant_tickers:
            log('INFO', 'polymarket',
                f'{probability:.0%} — {question[:60]}... [{",".join(relevant_tickers)}]')

    log('INFO', 'polymarket', f'Saved {saved} relevant markets')

if __name__ == '__main__':
    run()
