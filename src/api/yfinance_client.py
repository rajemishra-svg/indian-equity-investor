"""Yahoo Finance client — fallback data source for NSE stock quotes and valuation data.

Used when NSE India API returns 403 (non-browser environment).
Prices are ~15–20 min delayed, acceptable for long-term investment analysis.
NSE tickers map to Yahoo Finance symbols by appending '.NS' (e.g. RELIANCE → RELIANCE.NS).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import structlog
import yfinance as yf

from src.models import StockQuote, ValuationData


log = structlog.get_logger(__name__)

# Tickers that trade on BSE only or have non-standard Yahoo suffixes
_BSE_ONLY: set[str] = set()


def _to_yf_symbol(ticker: str) -> str:
    """Convert NSE ticker to Yahoo Finance symbol."""
    return f"{ticker.upper()}.NS"


def _safe_float(value: object) -> Optional[float]:
    """Return float or None for missing/infinite Yahoo Finance values."""
    try:
        f = float(value)  # type: ignore[arg-type]
        return None if (f != f or abs(f) > 1e15) else f  # NaN / Inf guard
    except (TypeError, ValueError):
        return None


class YFinanceClient:
    """Async wrapper around the synchronous yfinance library.

    Methods run yfinance calls in a thread-pool executor so they don't block
    the asyncio event loop.
    """

    async def get_stock_quote(self, ticker: str) -> Optional[StockQuote]:
        """Fetch a stock quote for an NSE ticker via Yahoo Finance.

        Returns StockQuote or None on failure.
        """
        yf_symbol = _to_yf_symbol(ticker)
        try:
            fast_info = await asyncio.get_event_loop().run_in_executor(
                None, lambda: yf.Ticker(yf_symbol).fast_info
            )
            cmp = _safe_float(fast_info.get("lastPrice") or fast_info.get("last_price"))
            if cmp is None or cmp == 0:
                log.warning("yfinance_no_price", ticker=ticker, yf_symbol=yf_symbol)
                return None

            market_cap = _safe_float(
                fast_info.get("marketCap") or fast_info.get("market_cap")
            )
            market_cap_cr = market_cap / 1e7 if market_cap else 0.0

            w52_high = _safe_float(
                fast_info.get("fiftyTwoWeekHigh") or fast_info.get("fifty_two_week_high")
            ) or 0.0
            w52_low = _safe_float(
                fast_info.get("fiftyTwoWeekLow") or fast_info.get("fifty_two_week_low")
            ) or 0.0
            dma_200 = _safe_float(
                fast_info.get("twoHundredDayAverage") or fast_info.get("two_hundred_day_average")
            )

            log.info(
                "yfinance_quote_ok",
                ticker=ticker,
                cmp=cmp,
                market_cap_cr=round(market_cap_cr, 1),
            )
            return StockQuote(
                ticker=ticker,
                company_name=ticker,  # fast_info doesn't have company name; good enough
                cmp=cmp,
                w52_high=w52_high,
                w52_low=w52_low,
                dma_200=dma_200,
                market_cap_cr=market_cap_cr,
                exchange="NSE",
                data_timestamp=datetime.now(timezone.utc),
                is_stale=True,  # Yahoo data is ~15-20 min delayed
            )
        except Exception as exc:
            log.warning("yfinance_quote_failed", ticker=ticker, error=str(exc))
            return None

    async def get_valuation_data(self, ticker: str) -> Optional[ValuationData]:
        """Fetch valuation multiples for an NSE ticker via Yahoo Finance.

        Returns ValuationData with pe_current, pbv_current, peg_ratio populated.
        Historical percentile data (pe_10y_percentile etc.) is not available from
        Yahoo Finance and will remain None.
        """
        yf_symbol = _to_yf_symbol(ticker)
        try:
            info = await asyncio.get_event_loop().run_in_executor(
                None, lambda: yf.Ticker(yf_symbol).info
            )
            if not info or info.get("quoteType") == "NONE":
                log.warning("yfinance_no_info", ticker=ticker)
                return None

            pe = _safe_float(info.get("trailingPE"))
            pbv = _safe_float(info.get("priceToBook"))
            peg = _safe_float(info.get("pegRatio"))
            fcf_yield = None
            ev_ebitda = _safe_float(info.get("enterpriseToEbitda"))
            shares = _safe_float(info.get("sharesOutstanding"))
            shares_cr = shares / 1e7 if shares else None

            flags: list[str] = []
            if pe is None:
                flags.append("[DATA UNVERIFIED: pe_current — not available from Yahoo Finance]")
            if pbv is None:
                flags.append("[DATA UNVERIFIED: pbv_current — not available from Yahoo Finance]")

            log.info("yfinance_valuation_ok", ticker=ticker, pe=pe, pbv=pbv)
            return ValuationData(
                pe_current=pe,
                pbv_current=pbv,
                peg_ratio=peg,
                ev_ebitda_current=ev_ebitda,
                fcf_yield_pct=fcf_yield,
                shares_outstanding_cr=shares_cr,
                data_flags=flags,
            )
        except Exception as exc:
            log.warning("yfinance_valuation_failed", ticker=ticker, error=str(exc))
            return None

    # Context manager support (no-op: yfinance has no session to manage)
    async def __aenter__(self) -> "YFinanceClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass
