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


def _compute_pe_percentile(
    price_hist: object,
    income_stmt: object,
    current_pe: float,
    shares: Optional[float],
) -> Optional[float]:
    """Compute approximate 5Y trailing-PE percentile from historical data.

    Assigns each monthly closing price to the contemporaneous annual EPS
    (derived from the income statement) and builds a PE observation series.
    Returns the percentile rank of *current_pe* within that series, so a
    value of 20 means the stock is cheaper than 80 % of its own history.

    Returns None when data is insufficient (< 12 monthly observations).
    Always silently returns None on any exception so callers never break.
    """
    try:
        import pandas as pd  # already a yfinance dependency; safe to import here

        if not isinstance(price_hist, pd.DataFrame) or price_hist.empty:
            return None
        if not isinstance(income_stmt, pd.DataFrame) or income_stmt.empty:
            return None
        if shares is None or shares <= 0 or current_pe <= 0:
            return None

        # Find the Net Income row (label varies slightly across yfinance versions)
        ni_row = None
        for candidate in ("Net Income", "Net Income Common Stockholders", "NetIncome"):
            if candidate in income_stmt.index:
                ni_row = income_stmt.loc[candidate]
                break
        if ni_row is None:
            return None

        pe_observations: list[float] = []

        for period_date, ni in ni_row.items():
            if pd.isna(ni) or float(ni) <= 0:
                continue
            eps = float(ni) / shares
            if eps <= 0:
                continue
            period_ts = pd.Timestamp(period_date)
            # Use monthly prices in the 12-month window ending at this fiscal year-end
            # (+ 3-month lead to account for reporting lag).
            window = price_hist[
                (price_hist.index >= period_ts - pd.DateOffset(months=12))
                & (price_hist.index <= period_ts + pd.DateOffset(months=3))
            ]["Close"]
            for price in window:
                pe = float(price) / eps
                if 3.0 < pe < 200.0:  # sanity bounds — filters data errors
                    pe_observations.append(pe)

        if len(pe_observations) < 12:  # need at least ~1 year of monthly data
            return None

        count_below = sum(1 for pe in pe_observations if pe < current_pe)
        return round(count_below / len(pe_observations) * 100.0, 1)

    except Exception:  # never let PE computation break the caller
        return None


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


def _compute_volume_trend_sync(hist_30d: object) -> Optional[str]:
    """Determine whether volume is declining on down-price days (P3-4).

    Compares average volume on red-candle days (Close < Open) against the
    overall median volume.  Declining volume on selling days is a bullish
    signal — institutions are not distributing aggressively.

    Returns "declining" | "stable" | "increasing" | None.
    Always silently returns None on any failure.
    """
    try:
        import pandas as pd

        if not isinstance(hist_30d, pd.DataFrame) or hist_30d.empty:
            return None
        if len(hist_30d) < 10:
            return None

        down_days = hist_30d[hist_30d["Close"] < hist_30d["Open"]]
        if len(down_days) < 3:
            return None

        median_vol = float(hist_30d["Volume"].median())
        if median_vol == 0:
            return None

        avg_down_vol = float(down_days["Volume"].mean())
        ratio = avg_down_vol / median_vol

        if ratio < 0.80:
            return "declining"    # healthy — low selling pressure on bad days
        elif ratio > 1.20:
            return "increasing"   # distribution — heavy selling on down days
        else:
            return "stable"
    except Exception:
        return None


class YFinanceClient:
    """Async wrapper around the synchronous yfinance library.

    Methods run yfinance calls in a thread-pool executor so they don't block
    the asyncio event loop.
    """

    async def get_stock_quote(self, ticker: str) -> Optional[StockQuote]:
        """Fetch a stock quote for an NSE ticker via Yahoo Finance.

        Returns StockQuote or None on failure.  Also computes:
          • avg_daily_value_cr  — 3-month average daily traded value in ₹ Cr (P2-5)
          • volume_trend_down_days — whether volume is declining on down-price days (P3-4)
        """
        yf_symbol = _to_yf_symbol(ticker)
        try:
            def _fetch_quote_data():
                t = yf.Ticker(yf_symbol)
                fi = t.fast_info
                # Also grab 30-day daily history for volume trend (P3-4).
                # Failures here are non-fatal — silently return None for hist.
                try:
                    hist_30d = t.history(period="30d", interval="1d")
                except Exception:
                    hist_30d = None
                return fi, hist_30d

            fast_info, hist_30d = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_quote_data
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

            # P2-5: Average daily traded value (3-month avg volume × CMP)
            avg_vol = _safe_float(
                fast_info.get("threeMonthAverageVolume")
                or fast_info.get("three_month_average_volume")
            )
            avg_daily_value_cr = round(avg_vol * cmp / 1e7, 2) if avg_vol and cmp else None

            # P3-4: Volume trend on down-price days
            volume_trend_down_days = _compute_volume_trend_sync(hist_30d)

            log.info(
                "yfinance_quote_ok",
                ticker=ticker,
                cmp=cmp,
                market_cap_cr=round(market_cap_cr, 1),
                avg_daily_value_cr=avg_daily_value_cr,
                volume_trend=volume_trend_down_days,
            )
            return StockQuote(
                ticker=ticker,
                company_name=ticker,
                cmp=cmp,
                w52_high=w52_high,
                w52_low=w52_low,
                dma_200=dma_200,
                market_cap_cr=market_cap_cr,
                exchange="NSE",
                data_timestamp=datetime.now(timezone.utc),
                is_stale=True,
                avg_daily_value_cr=avg_daily_value_cr,
                volume_trend_down_days=volume_trend_down_days,
            )
        except Exception as exc:
            log.warning("yfinance_quote_failed", ticker=ticker, error=str(exc))
            return None

    async def get_nifty50(self) -> tuple[float, float]:
        """Fetch Nifty 50 current level and 52-week high via Yahoo Finance.

        Uses the ^NSEI ticker (Yahoo Finance symbol for Nifty 50).
        Note: fast_info on index tickers exposes attributes, not dict keys.

        Returns:
            Tuple of (current_level, 52w_high).

        Raises:
            ValueError: If data is missing or zero.
        """
        try:
            fast_info = await asyncio.get_event_loop().run_in_executor(
                None, lambda: yf.Ticker("^NSEI").fast_info
            )
            # fast_info exposes snake_case attributes (lastPrice → last_price)
            current = _safe_float(getattr(fast_info, "last_price", None))
            year_high = _safe_float(getattr(fast_info, "year_high", None))
            if not current or not year_high:
                raise ValueError("Missing Nifty 50 data from Yahoo Finance")
            log.info("yfinance_nifty50_ok", current=current, year_high=year_high)
            return current, year_high
        except Exception as exc:
            log.warning("yfinance_nifty50_failed", error=str(exc))
            raise

    async def get_valuation_data(self, ticker: str) -> Optional[ValuationData]:
        """Fetch valuation multiples and compute 5Y PE percentile for an NSE ticker.

        Returns ValuationData with pe_current, pbv_current, peg_ratio, and
        pe_10y_percentile (computed from 5Y monthly price history + annual EPS).
        The percentile computation is best-effort — returns None if yfinance
        cannot supply sufficient history (e.g. recently listed companies).
        """
        yf_symbol = _to_yf_symbol(ticker)
        try:
            def _fetch_all():
                t = yf.Ticker(yf_symbol)
                info = t.info
                # Fetch history + income statement for PE percentile in the same
                # executor call so we only create one Ticker object.
                try:
                    price_hist = t.history(period="5y", interval="1mo")
                    income_stmt = t.income_stmt
                    fast_info = t.fast_info
                except Exception:
                    price_hist = None
                    income_stmt = None
                    fast_info = None
                return info, price_hist, income_stmt, fast_info

            info, price_hist, income_stmt, fast_info = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_all
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

            # P1-2: Compute 5Y trailing-PE percentile from price history + EPS.
            # _compute_pe_percentile() is fully defensive — returns None on any failure.
            pe_10y_percentile: Optional[float] = None
            if pe is not None and pe > 0:
                pe_10y_percentile = _compute_pe_percentile(price_hist, income_stmt, pe, shares)
                if pe_10y_percentile is not None:
                    log.info(
                        "yfinance_pe_percentile_ok",
                        ticker=ticker,
                        pe=pe,
                        pe_percentile=pe_10y_percentile,
                    )
                else:
                    log.debug(
                        "yfinance_pe_percentile_unavailable",
                        ticker=ticker,
                        reason="insufficient_history",
                    )

            flags: list[str] = []
            if pe is None:
                flags.append("[DATA UNVERIFIED: pe_current — not available from Yahoo Finance]")
            if pbv is None:
                flags.append("[DATA UNVERIFIED: pbv_current — not available from Yahoo Finance]")
            if pe_10y_percentile is None:
                flags.append(
                    "[DATA UNVERIFIED: pe_10y_percentile — insufficient 5Y history from Yahoo Finance; "
                    "cross-check Trendlyne for historical PE band]"
                )

            log.info("yfinance_valuation_ok", ticker=ticker, pe=pe, pbv=pbv)
            return ValuationData(
                pe_current=pe,
                pbv_current=pbv,
                peg_ratio=peg,
                ev_ebitda_current=ev_ebitda,
                fcf_yield_pct=fcf_yield,
                shares_outstanding_cr=shares_cr,
                pe_10y_percentile=pe_10y_percentile,
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
