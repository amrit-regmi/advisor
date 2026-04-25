-- AI Financial Advisor — full schema
-- Run: psql -U advisor_user -d advisor -f schema.sql

CREATE TABLE IF NOT EXISTS prices (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    date DATE NOT NULL,
    open DECIMAL(12,4),
    high DECIMAL(12,4),
    low DECIMAL(12,4),
    close DECIMAL(12,4),
    volume BIGINT,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ticker, date)
);

CREATE TABLE IF NOT EXISTS news_sentiment (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20),
    sector VARCHAR(100),
    date DATE NOT NULL,
    article_count INTEGER,
    avg_tone DECIMAL(8,4),
    positive_count INTEGER,
    negative_count INTEGER,
    source_countries TEXT,
    top_themes TEXT,
    mention_velocity DECIMAL(8,4) DEFAULT 1.0,
    tone_trajectory DECIMAL(8,4) DEFAULT 0.0,
    is_accelerating BOOLEAN DEFAULT FALSE,
    finbert_sentiment_score DECIMAL(6,4),
    finbert_sentiment_label VARCHAR(20),
    finbert_confidence DECIMAL(6,4),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ticker, date)
);

CREATE TABLE IF NOT EXISTS polymarket_signals (
    id SERIAL PRIMARY KEY,
    market_id VARCHAR(100),
    question TEXT,
    probability DECIMAL(6,4),
    date DATE NOT NULL,
    relevant_tickers TEXT,
    relevant_sectors TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS macro_data (
    id SERIAL PRIMARY KEY,
    series_id VARCHAR(50) NOT NULL,
    date DATE NOT NULL,
    value DECIMAL(16,6),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(series_id, date)
);

CREATE TABLE IF NOT EXISTS holdings (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    shares DECIMAL(12,4) NOT NULL,
    avg_buy_price DECIMAL(12,4),
    currency VARCHAR(10) DEFAULT 'EUR',
    exchange VARCHAR(50),
    bought_date DATE,
    notes TEXT,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    action VARCHAR(10) NOT NULL,
    shares DECIMAL(12,4) NOT NULL,
    price DECIMAL(12,4) NOT NULL,
    currency VARCHAR(10) DEFAULT 'EUR',
    trade_date DATE NOT NULL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recommendations (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    ticker VARCHAR(20),
    action VARCHAR(10),
    confidence DECIMAL(5,4),
    reasoning TEXT,
    debate_trace TEXT,
    bull_case TEXT,
    bear_case TEXT,
    key_risks TEXT,
    time_horizon VARCHAR(50),
    signal_sources TEXT,
    acted_on BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS discovery_candidates (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    ticker VARCHAR(20) NOT NULL,
    direction VARCHAR(10),
    event_type VARCHAR(50),
    gdelt_score DECIMAL(8,4),
    polymarket_score DECIMAL(8,4),
    momentum_score DECIMAL(8,4),
    total_score DECIMAL(8,4),
    quant_score DECIMAL(8,4),
    blended_score DECIMAL(8,4),
    selected_for_analysis BOOLEAN DEFAULT FALSE,
    reason TEXT,
    currency VARCHAR(10),
    fx_risk VARCHAR(10),
    tax_notes TEXT,
    dividend_withholding_pct INTEGER,
    eligible BOOLEAN DEFAULT TRUE,
    analyzed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS api_usage (
    id SERIAL PRIMARY KEY,
    api_name VARCHAR(50) NOT NULL,
    date DATE NOT NULL,
    call_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(api_name, date)
);

CREATE TABLE IF NOT EXISTS system_logs (
    id SERIAL PRIMARY KEY,
    level VARCHAR(20),
    component VARCHAR(50),
    message TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS universe (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL UNIQUE,
    company_name VARCHAR(200),
    exchange VARCHAR(50),
    sector VARCHAR(100),
    country VARCHAR(10),
    asset_type VARCHAR(20) DEFAULT 'stock',
    validated BOOLEAN DEFAULT FALSE,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS watchlist (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL UNIQUE,
    company_name VARCHAR(200),
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_settings (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) NOT NULL UNIQUE,
    value TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS performance_history (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    portfolio_value DECIMAL(14,2),
    cash DECIMAL(14,2),
    positions TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(date)
);

CREATE TABLE IF NOT EXISTS strategy_metrics (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    sharpe DECIMAL(8,4),
    max_drawdown DECIMAL(8,4),
    hit_rate DECIMAL(6,4),
    cumulative_pnl DECIMAL(14,2),
    total_recommendations INTEGER,
    profitable_buys INTEGER,
    loss_avoided_sells INTEGER,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(date)
);

-- Default settings
INSERT INTO user_settings (key, value) VALUES
    ('nordnet_cash_eur', '0'),
    ('monthly_investment_budget_eur', '500'),
    ('monthly_invested_this_month', '0'),
    ('max_stocks_per_day', '5'),
    ('min_confidence', '0.6')
ON CONFLICT (key) DO NOTHING;
