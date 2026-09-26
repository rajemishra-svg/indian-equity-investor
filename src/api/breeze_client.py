"""ICICI Direct Breeze Connect client — real-time NSE stock quotes.

Sits between NSEClient (primary, browser-session-spoofed) and YFinanceClient
(last-resort, 15–20 min delayed) in the quote fallback chain.  Breeze uses an
authenticated official API so it is not affected by NSE's bot detection.

Authentication (once per trading day):
  1. Run `investor breeze-login` — prints the OAuth URL
  2. Log in with ICICI Direct credentials in your browser
  3. Copy the `apisession=XXXX` token from the redirect URL bar
  4. Paste it at the prompt → cached at ~/.tradedesk_breeze_session

The session cache is shared with the Multi-Agent Trading System if both are
installed; one daily login serves both pipelines.

When BREEZE_API_KEY / BREEZE_API_SECRET are not set, or no valid session is
cached, `get_stock_quote` returns None immediately so the pipeline falls through
to yfinance without any error.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from src.api.technicals import compute_52w_range, compute_dma, compute_rsi
from src.models import StockQuote

log = structlog.get_logger(__name__)

_API_KEY: str = os.getenv("BREEZE_API_KEY", "")
_API_SECRET: str = os.getenv("BREEZE_API_SECRET", "")
_SESSION_CACHE = Path.home() / ".tradedesk_breeze_session"

# Module-level singleton — initialised lazily on first get_stock_quote call.
_CLIENT: Any = None
_CLIENT_READY: bool = False  # True once generate_session has completed
# NSE ticker → Breeze ISEC short code cache (e.g. "RELIANCE" → "RELIND").
_ISEC_CODE_CACHE: dict[str, str] = {}


def is_configured() -> bool:
    return bool(_API_KEY and _API_SECRET)


def load_cached_token() -> str | None:
    try:
        if not _SESSION_CACHE.exists():
            return None
        data = json.loads(_SESSION_CACHE.read_text())
        if data.get("date") == date.today().isoformat():
            return data.get("token")
    except Exception:
        pass
    return None


def save_session_token(token: str) -> None:
    _SESSION_CACHE.write_text(
        json.dumps({"date": date.today().isoformat(), "token": token.strip()})
    )


def get_login_url() -> str:
    return f"https://api.icicidirect.com/apiuser/home?AppKey={_API_KEY}"


def _get_isec_code(client: Any, nse_ticker: str) -> str:
    """Resolve an NSE ticker to the Breeze internal ISEC short code.

    Breeze's get_quotes / get_historical_data_v2 accept the ISEC code (e.g.
    "RELIND") not the standard NSE symbol ("RELIANCE").  Results are cached
    for the process lifetime so each ticker only calls get_names() once.
    """
    if nse_ticker in _ISEC_CODE_CACHE:
        return _ISEC_CODE_CACHE[nse_ticker]
    try:
        resp = client.get_names(exchange_code="NSE", stock_code=nse_ticker)
        isec = (resp or {}).get("isec_stock_code", "").strip()
        if isec:
            _ISEC_CODE_CACHE[nse_ticker] = isec
            return isec
    except Exception as exc:
        log.debug("breeze_get_names_failed", ticker=nse_ticker, error=str(exc))
    _ISEC_CODE_CACHE[nse_ticker] = nse_ticker
    return nse_ticker


def _safe_float(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
        return None if (f != f or abs(f) > 1e15) else f
    except (TypeError, ValueError):
        return None


def _init_client_sync() -> Any | None:
    """Initialise Breeze client synchronously (called from executor)."""
    global _CLIENT, _CLIENT_READY
    if _CLIENT_READY:
        return _CLIENT

    token = load_cached_token()
    if not token:
        return None
    try:
        from breeze_connect import BreezeConnect  # type: ignore[import-untyped]
        client = BreezeConnect(api_key=_API_KEY)
        # generate_session downloads the scrip master CSV (~3 s) — must be in executor
        client.generate_session(api_secret=_API_SECRET, session_token=token)
        _CLIENT = client
        _CLIENT_READY = True
        log.info("breeze_session_ready")
        return client
    except Exception as exc:
        log.warning("breeze_session_init_failed", error=str(exc))
        return None


async def _get_client() -> Any | None:
    if not is_configured():
        return None
    if _CLIENT_READY:
        return _CLIENT
    return await asyncio.get_event_loop().run_in_executor(None, _init_client_sync)


def _parse_history(rows: list[dict]) -> dict:
    """Compute 52W high/low, 200 DMA, RSI-14, avg daily value, and volume trend.

    Expects rows sorted oldest-first, each with string fields:
      open, high, low, close, volume, datetime
    """
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    daily_values: list[float] = []  # close × volume per day (₹)
    vol_bars: list[tuple[float, bool]] = []  # (volume, is_down_day)

    for row in rows:
        c = _safe_float(row.get("close"))
        h = _safe_float(row.get("high"))
        lo = _safe_float(row.get("low"))
        o = _safe_float(row.get("open"))
        v = _safe_float(row.get("volume"))
        if c is None:
            continue
        closes.append(c)
        if h is not None:
            highs.append(h)
        if lo is not None:
            lows.append(lo)
        if v is not None:
            daily_values.append(c * v)
            vol_bars.append((v, o is not None and c < o))

    # History covers ~400 calendar days; the 52W range uses only the last 252 bars.
    w52_high, w52_low = compute_52w_range(highs, lows)
    dma_200 = compute_dma(closes)  # None until 200 closes exist

    # 3-month avg daily traded value (≈ last 63 trading days)
    recent_values = daily_values[-63:]
    avg_daily_value_cr = round(sum(recent_values) / len(recent_values) / 1e7, 2) if recent_values else None

    # Volume trend over the last ~30 calendar days (21 bars), matching yfinance:
    # compare avg vol on down-price days vs overall median
    recent_bars = vol_bars[-21:]
    all_vols = [v for v, _ in recent_bars]
    down_vols = [v for v, is_down in recent_bars if is_down]
    volume_trend: str | None = None
    if len(all_vols) >= 10 and len(down_vols) >= 3:
        all_vols_sorted = sorted(all_vols)
        median_vol = all_vols_sorted[len(all_vols_sorted) // 2]
        avg_down_vol = sum(down_vols) / len(down_vols)
        ratio = avg_down_vol / median_vol if median_vol > 0 else 1.0
        volume_trend = "declining" if ratio < 0.80 else "increasing" if ratio > 1.20 else "stable"

    return {
        "w52_high": w52_high or 0.0,
        "w52_low": w52_low or 0.0,
        "dma_200": dma_200,
        "rsi_14": compute_rsi(closes),
        "avg_daily_value_cr": avg_daily_value_cr,
        "volume_trend_down_days": volume_trend,
    }


def _fetch_quote_sync(client: Any, ticker: str) -> StockQuote | None:
    """Run both Breeze calls synchronously (designed for run_in_executor)."""
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist)
    today = now_ist.date()

    # Breeze requires the internal ISEC short code, not the NSE ticker.
    isec = _get_isec_code(client, ticker)

    # 252 trading days ≈ 1 year; request 400 calendar days to be safe
    from_date = (today - timedelta(days=400)).strftime("%Y-%m-%dT00:00:00.000Z")
    to_date = today.strftime("%Y-%m-%dT23:59:59.000Z")

    # --- 1. Real-time quote ---
    ltp: float | None = None
    try:
        resp = client.get_quotes(
            stock_code=isec,
            exchange_code="NSE",
            expiry_date="",
            product_type="cash",
            right="",
            strike_price="",
        )
        if isinstance(resp, dict) and resp.get("Status") == 200:
            rows = resp.get("Success") or []
            if rows:
                ltp = _safe_float(rows[0].get("ltp"))
    except Exception as exc:
        log.warning("breeze_get_quotes_failed", ticker=ticker, error=str(exc))

    if ltp is None:
        return None

    # --- 2. Daily history for 52W high/low, 200 DMA, avg daily value, vol trend ---
    hist_metrics: dict = {}
    try:
        hist_resp = client.get_historical_data_v2(
            interval="1day",
            from_date=from_date,
            to_date=to_date,
            stock_code=isec,
            exchange_code="NSE",
        )
        if isinstance(hist_resp, dict) and hist_resp.get("Status") == 200:
            hist_rows = hist_resp.get("Success") or []
            if hist_rows:
                hist_metrics = _parse_history(hist_rows)
    except Exception as exc:
        log.warning("breeze_historical_failed", ticker=ticker, error=str(exc))

    return StockQuote(
        ticker=ticker,
        company_name=ticker,                          # Breeze doesn't expose this
        cmp=ltp,
        w52_high=hist_metrics.get("w52_high") or 0.0,
        w52_low=hist_metrics.get("w52_low") or 0.0,
        dma_200=hist_metrics.get("dma_200"),
        market_cap_cr=0.0,                            # not available from Breeze
        exchange="NSE",
        data_timestamp=datetime.now(UTC),
        is_stale=False,                               # real-time price
        avg_daily_value_cr=hist_metrics.get("avg_daily_value_cr"),
        volume_trend_down_days=hist_metrics.get("volume_trend_down_days"),
        rsi_14=hist_metrics.get("rsi_14"),
    )


class BreezeClient:
    """Async Breeze Connect client — real-time NSE stock quotes.

    No-op async context manager: session is initialised lazily on first
    get_stock_quote call so pipeline startup is not penalised when Breeze
    is not configured or the session is stale.
    """

    async def __aenter__(self) -> BreezeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def get_stock_quote(self, ticker: str) -> StockQuote | None:
        """Return a real-time StockQuote via Breeze, or None on any failure.

        Two calls under the hood (both in executor, blocking-safe):
          1. get_quotes()            → live LTP
          2. get_historical_data_v2  → 52W high/low, 200 DMA, avg daily value,
                                       volume trend on down-price days

        Market cap is not available from Breeze — set to 0.0.  The pipeline
        reads market_cap_cr from FinancialMetrics (Screener) as a fallback.
        """
        if not is_configured():
            return None

        client = await _get_client()
        if client is None:
            return None

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_quote_sync, client, ticker.upper()
            )
            if result is not None:
                log.info(
                    "breeze_quote_ok",
                    ticker=ticker,
                    cmp=result.cmp,
                    dma_200=result.dma_200,
                    avg_daily_value_cr=result.avg_daily_value_cr,
                )
            return result
        except Exception as exc:
            log.warning("breeze_quote_failed", ticker=ticker, error=str(exc))
            return None
