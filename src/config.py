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

    log_level: str = "INFO"
    log_format: str = "json"  # "json" or "console"

    db_path: str = "investor.db"  # SQLite database file


settings = Settings()
