"""
YFinance Data Client — free market data, zero API cost.

Replaces RobinhoodMarketDataClient for all market data fetching.
Robinhood MCP is now only used for actual order placement.

Results are cached in-process (one download per symbol per run).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

# In-process cache: avoids re-downloading the same symbol within one run.
# Key: "SYMBOL:period_string" -> pd.DataFrame
_CACHE: Dict[str, pd.DataFrame] = {}


def _fetch(yf_symbol: str, period: str = "3mo") -> pd.DataFrame:
    """
    Download OHLCV data via yfinance, cache it, return sorted oldest-first.
    Uses ticker.history() to avoid the MultiIndex issue in yf.download().
    """
    key = f"{yf_symbol}:{period}"
    if key in _CACHE:
        return _CACHE[key]
    try:
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period=period, interval="1d", auto_adjust=True)
        df = df.sort_index()
        # Normalize index to timezone-naive dates
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        _CACHE[key] = df
        return df
    except Exception as e:
        logger.error("yfinance fetch error for %s: %s", yf_symbol, e)
        return pd.DataFrame()


def _df_to_bars(df: pd.DataFrame, days: int) -> List[Dict]:
    """Slice a DataFrame to the last `days` calendar days and convert to bar dicts."""
    if df.empty:
        return []
    cutoff = pd.Timestamp(date.today() - timedelta(days=days))
    sliced = df[df.index >= cutoff]
    result = []
    for ts, row in sliced.iterrows():
        result.append({
            "date":   ts.strftime("%Y-%m-%d"),
            "open":   float(row.get("Open",   0.0)),
            "high":   float(row.get("High",   0.0)),
            "low":    float(row.get("Low",    0.0)),
            "close":  float(row.get("Close",  0.0)),
            "volume": float(row.get("Volume", 0.0)),
        })
    return result


def _period_for_days(days: int) -> str:
    """
    Map a requested trading-day count to a yfinance period string.
    We intentionally over-fetch so MA50 always has enough bars:
    calendar days are ~30% longer than trading days, so multiply by 1.5 + buffer.
    """
    trading_days_needed = max(days, 55)  # never fetch less than 55 trading days
    if trading_days_needed <= 21:
        return "1mo"
    if trading_days_needed <= 63:
        return "3mo"
    return "6mo"


class YFinanceDataClient:
    """
    Implements the MarketDataClient protocol from research_engine.py.
    All data comes from Yahoo Finance — completely free, no Anthropic API usage.
    """

    def get_price_history(self, symbol: str, days: int) -> List[Dict]:
        """Return [{date, open, high, low, close, volume}, ...] oldest first."""
        yf_symbol = "^VIX" if symbol == "VIX" else symbol
        # Always fetch 3mo minimum so MA50 (50 bars) always has enough data.
        # Return ALL bars from that period — the research engine uses the last N anyway.
        period = _period_for_days(days)
        df = _fetch(yf_symbol, period)
        return _df_to_bars(df, days=90)  # return full 3mo window, never truncate

    def get_current_price(self, symbol: str) -> float:
        """Return the most recent closing price."""
        bars = self.get_price_history(symbol, days=5)
        return bars[-1]["close"] if bars else 0.0

    def get_upcoming_earnings(self, symbol: str) -> Optional[str]:
        """
        Return next earnings date as an ISO string (YYYY-MM-DD), or None.
        Tries ticker.calendar first, falls back to ticker.info.
        """
        try:
            ticker = yf.Ticker(symbol)
            cal = ticker.calendar
            # yfinance ≥ 0.2 returns a dict; older versions returned a DataFrame
            if isinstance(cal, dict):
                dates = cal.get("Earnings Date", [])
                if dates:
                    dt = dates[0]
                    if hasattr(dt, "date"):
                        return dt.date().isoformat()
                    return str(dt)[:10]
            elif isinstance(cal, pd.DataFrame) and not cal.empty:
                # Columns are dates; first column is next event
                col = cal.columns[0]
                if hasattr(col, "strftime"):
                    return col.strftime("%Y-%m-%d")
        except Exception as e:
            logger.debug("get_upcoming_earnings(%s) error: %s", symbol, e)
        return None

    def get_recent_news(self, symbol: str, days: int) -> List[Dict]:
        # News is not used in any filter logic — return empty to avoid extra I/O
        return []

    def get_vix_data(self, days: int) -> List[Dict]:
        """Return VIX price bars for the last `days` calendar days."""
        bars = self.get_price_history("VIX", days=days)
        if bars:
            return bars
        # Fallback: single neutral bar so the macro check doesn't crash
        return [{"date": date.today().isoformat(), "close": 20.0,
                 "open": 20.0, "high": 20.0, "low": 20.0, "volume": 0.0}]

    def get_intraday_volume(self, symbol: str) -> Optional[float]:
        """
        Return today's cumulative traded volume so far using 5-min bars.
        Returns None if market hasn't opened or data is unavailable.
        """
        try:
            yf_symbol = "^VIX" if symbol == "VIX" else symbol
            ticker = yf.Ticker(yf_symbol)
            df = ticker.history(period="1d", interval="5m", auto_adjust=True)
            if df.empty:
                return None
            # Sum only bars from today (ignore any yesterday spillover)
            et = ZoneInfo("America/New_York")
            today_et = datetime.now(et).date()
            if df.index.tz is not None:
                df_today = df[df.index.tz_convert(et).normalize().dt.date == today_et]
            else:
                df_today = df[df.index.normalize().date == today_et]
            if df_today.empty:
                return None
            return float(df_today["Volume"].sum())
        except Exception as e:
            logger.debug("get_intraday_volume(%s) error: %s", symbol, e)
            return None

    def get_popular_watchlist_symbols(self) -> List[str]:
        # No equivalent in yfinance — weekly review works fine without new candidates
        return []
