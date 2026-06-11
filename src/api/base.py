"""Base async HTTP client with retry logic."""
from __future__ import annotations

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import settings


class BaseHTTPClient:
    """Async HTTP client with retry and session management."""

    def __init__(self, base_url: str, timeout: float | None = None):
        self.base_url = base_url
        self.timeout = timeout or settings.http_timeout
        self._client: httpx.AsyncClient | None = None
        self.log = structlog.get_logger(self.__class__.__name__)

    async def __aenter__(self) -> BaseHTTPClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers=self._default_headers(),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _default_headers(self) -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
        reraise=True,
    )
    async def get(self, path: str, **kwargs) -> httpx.Response:
        """Perform a GET request with automatic retry on transient errors."""
        if self._client is None:
            raise RuntimeError("Client not initialised — use async context manager")
        self.log.debug("http_request", method="GET", path=path)
        response = await self._client.get(path, **kwargs)
        response.raise_for_status()
        self.log.debug("http_response", status=response.status_code, path=path)
        return response
