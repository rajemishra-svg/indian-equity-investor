"""SQLite schema definition and database initialisation."""
from __future__ import annotations

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    company_name TEXT,
    analysis_date TEXT NOT NULL,       -- ISO date YYYY-MM-DD
    market_mode TEXT,
    sector_name TEXT,
    cap_size TEXT,
    cmp REAL,
    market_cap_cr REAL,

    -- Step gate results
    pre_screen_score INTEGER,
    pre_screen_max INTEGER DEFAULT 9,
    pre_screen_gate TEXT,
    governance_score INTEGER,
    governance_max INTEGER DEFAULT 15,
    governance_gate TEXT,
    governance_sub_scores TEXT,        -- JSON {"pledging":2,"audit":2,...}
    governance_triggers TEXT,          -- JSON array of immediate trigger names
    financial_score INTEGER,
    financial_max INTEGER DEFAULT 7,
    financial_gate TEXT,
    financial_triggers TEXT,           -- JSON array of hard trigger descriptions
    valuation_gate TEXT,
    valuation_methods_in_buy_zone INTEGER,
    mos_pct REAL,
    required_mos_pct REAL,
    dcf_intrinsic_weighted REAL,

    -- Growth mode fields
    analysis_mode TEXT DEFAULT 'value',         -- 'value' | 'growth'
    multibagger_total_score INTEGER,            -- 0-10 (growth mode only)
    multibagger_verdict TEXT,                   -- MULTIBAGGER_CANDIDATE | GROWTH_BUY | ...

    -- Final outcome
    terminated_at_step INTEGER,
    termination_reason TEXT,
    recommendation TEXT,
    conviction TEXT,
    watchlist_tier INTEGER,
    -- P2-2: DCF-derived buy target stored at WATCHLIST creation time so
    -- watchlist-alerts can compare live CMP without re-running the pipeline.
    target_buy_price REAL,
    investment_thesis TEXT,

    -- Audit trail
    all_data_flags TEXT,               -- JSON array
    error_tags TEXT,                   -- JSON array

    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(ticker, analysis_date)      -- one result per ticker per day (last-write wins)
);

CREATE TABLE IF NOT EXISTS data_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,       -- ISO date
    data_type TEXT NOT NULL,           -- 'quote' | 'financials' | 'governance' | 'valuation'
    source TEXT,                       -- 'nse' | 'screener' | 'yfinance' | etc.
    data_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(ticker, snapshot_date, data_type)
);

CREATE INDEX IF NOT EXISTS idx_analyses_ticker ON analyses(ticker);
CREATE INDEX IF NOT EXISTS idx_analyses_date ON analyses(analysis_date);
CREATE INDEX IF NOT EXISTS idx_analyses_recommendation ON analyses(recommendation);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_date ON data_snapshots(ticker, snapshot_date);

CREATE TABLE IF NOT EXISTS portfolio_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'default',
    ticker TEXT NOT NULL,
    company_name TEXT NOT NULL DEFAULT '',
    avg_cost REAL NOT NULL,
    quantity INTEGER NOT NULL,
    purchase_date TEXT NOT NULL,            -- ISO date YYYY-MM-DD
    allocation_pct REAL NOT NULL DEFAULT 0.0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolio_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'default',
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,                   -- 'BUY' | 'SELL'
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    txn_date TEXT NOT NULL,                 -- ISO date YYYY-MM-DD
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolio_tax (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'default',
    ticker TEXT NOT NULL,
    purchase_date TEXT NOT NULL,            -- ISO date YYYY-MM-DD
    ltcg_date TEXT NOT NULL,               -- 1 year after purchase_date
    avg_cost REAL NOT NULL DEFAULT 0.0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_holdings_user    ON portfolio_holdings(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON portfolio_transactions(user_id, txn_date);
CREATE INDEX IF NOT EXISTS idx_tax_user         ON portfolio_tax(user_id);

CREATE TABLE IF NOT EXISTS llm_costs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    executed_at        TEXT NOT NULL,          -- ISO datetime UTC
    subcommand         TEXT,                   -- analyze / scan / scan-prescreen / surveillance / ...
    ticker             TEXT,                   -- nullable; populated for analyze
    index_name         TEXT,                   -- nullable; populated for scan
    recommendation     TEXT,                   -- BUY / WATCHLIST / REJECT / PEER_SWITCH; nullable
    num_tickers        INTEGER,                -- for scan: how many full analyses ran
    cost_usd           REAL,
    cost_usd_sonnet    REAL,
    cost_usd_haiku     REAL,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    elapsed_seconds    REAL,
    status             TEXT                    -- success / error / partial
);

CREATE INDEX IF NOT EXISTS idx_llm_costs_executed_at  ON llm_costs(executed_at);
CREATE INDEX IF NOT EXISTS idx_llm_costs_subcommand   ON llm_costs(subcommand);
CREATE INDEX IF NOT EXISTS idx_llm_costs_ticker       ON llm_costs(ticker);
"""
