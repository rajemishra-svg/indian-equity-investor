"""Application configuration via pydantic-settings."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,  # treat empty env vars as unset; .env file value wins
    )

    anthropic_api_key: str = "dummy-key-for-tests"

    # --- Model routing ---
    # Sonnet for deep qualitative + agentic steps (2, 7); Haiku for structured/simple steps.
    # Haiku is ~15x cheaper per token than Sonnet; use it wherever reasoning depth is low.
    model_heavy: str = "claude-sonnet-4-6"           # Steps 2, 7 (agentic + complex reasoning)
    model_light: str = "claude-haiku-4-5-20251001"   # Steps 1, 4, 5, 8, 9 (structured JSON)

    # --- Per-step max_tokens ---
    # Tight limits prevent runaway output and reduce output-token billing.
    max_tokens: int = 4096               # default / agentic loop iterations
    max_tokens_mini: int = 256           # tiny JSON (capital allocation score: {"score":3})
    max_tokens_short: int = 600          # medium JSON (DCF, tailwind, premortem)
    max_tokens_thesis: int = 512         # narrative output (investment thesis)

    nse_base_url: str = "https://www.nseindia.com"
    bse_base_url: str = "https://www.bseindia.com"
    screener_base_url: str = "https://www.screener.in"
    trendlyne_base_url: str = "https://trendlyne.com"

    http_timeout: float = 30.0
    http_max_retries: int = 3

    # --- In-memory data cache TTLs (seconds) ---
    cache_ttl_quote: int = 3600       # 1 hour — prices update intraday
    cache_ttl_financials: int = 86400  # 24 hours — quarterly data

    # --- SQLite warm-cache TTL for prescreen data (hours) ---
    # Financials and shareholding change at most quarterly; 7 days lets repeated
    # scans within a week skip Screener entirely for already-seen tickers.
    # Set to 24 to match the in-memory TTL (conservative); raise to 168 (7 days)
    # for maximum speed on weekly scans.
    cache_ttl_db_financials_hours: int = 168   # 7 days — quarterly data
    cache_ttl_db_governance_hours: int = 168   # 7 days — shareholding pattern

    # --- Scan concurrency ---
    # Controls the asyncio semaphore in BatchScanner.prescreen_universe().
    # With SQLite warm cache active most tickers skip Screener entirely, so
    # higher concurrency is safe on warm runs. For cold first-run scans keep
    # at 8 or lower to avoid Screener.in 429s. Range: 3 (safe) – 15 (fast+warm).
    scan_concurrency: int = 8

    log_level: str = "INFO"
    log_format: str = "json"  # "json" or "console"

    db_path: str = "investor.db"  # SQLite database file

    # --- DCF WACC base rates by cap size (%) ---
    # Sector profiles add an adjustment on top via wacc_adjustment.
    wacc_large_cap: float = 13.0
    wacc_mid_cap: float = 15.0
    wacc_small_cap: float = 16.5
    wacc_terminal_growth: float = 6.0  # India long-run nominal GDP ~10%; sustainable share <70%

    # --- Tranche entry levels (relative to CMP) ---
    # T1 = CMP, T2 = CMP * (1 - t2_discount), T3 = CMP * (1 - t3_discount)
    tranche_t2_discount: float = 0.08   # 8% below CMP
    tranche_t3_discount: float = 0.15   # 15% below CMP

    # --- Stop-loss multipliers by cap size ---
    stop_loss_large_cap: float = 0.82
    stop_loss_mid_cap: float = 0.75
    stop_loss_small_cap: float = 0.70


settings = Settings()
