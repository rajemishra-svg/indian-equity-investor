"""In-memory TTL cache for prefetched market data.

Avoids redundant HTTP round-trips when the same ticker is analysed multiple times
within the same process lifetime (e.g. batch sector scans, correction-mode sweeps).

TTLs are intentionally conservative:
- Quote: 1 hour  — intraday prices change but don't need sub-minute freshness
- Financials: 24 hours — quarterly data; safe to reuse within a session
- Shareholding: 24 hours — same cadence as financials
- Valuation: 1 hour — P/E multiples shift with the market
"""
from __future__ import annotations

import time
from typing import Any, Optional, TypeVar

from src.logging_config import get_logger

T = TypeVar("T")
log = get_logger("data_cache")


class _CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: int) -> None:
        self.value = value
        self.expires_at = time.monotonic() + ttl


class DataCache:
    """Simple dict-backed TTL cache.  Not thread-safe (single-process async use only)."""

    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            del self._store[key]
            log.debug("cache_miss_expired", key=key)
            return None
        log.debug("cache_hit", key=key)
        return entry.value

    def set(self, key: str, value: Any, ttl: int) -> None:
        if value is None:
            return  # never cache None — let the real fetch happen on retry
        self._store[key] = _CacheEntry(value, ttl)
        log.debug("cache_set", key=key, ttl_seconds=ttl)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    @staticmethod
    def quote_key(ticker: str) -> str:
        return f"quote:{ticker}"

    @staticmethod
    def financials_key(ticker: str) -> str:
        return f"financials:{ticker}"

    @staticmethod
    def shareholding_key(ticker: str) -> str:
        return f"shareholding:{ticker}"

    @staticmethod
    def valuation_key(ticker: str) -> str:
        return f"valuation:{ticker}"


# Module-level singleton — shared across pipeline calls within the same process.
data_cache = DataCache()
