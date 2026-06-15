"""
Backtest — walks the exact same screening pipeline backwards through
2 years of history to measure whether the signals actually work.

Run:  python backtest.py

Uses yfinance for free historical data. No Anthropic API calls.
The BacktestDataClient feeds historical snapshots to the same
research_engine functions used in live trading — zero look-ahead bias.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from trading_agent import config
from trading_agent.research_engine import (
    MacroSnapshot, MacroState,
    technical_screen, macro_sentiment_check,
    _sma, _rsi_series, _detect_rsi_bounce, _detect_rsi_momentum,
    _find_support, _find_resistance,
    TechnicalSignal,
)

# ── configuration ──────────────────────────────────────────────────────────────
SYMBOLS      = config.WATCHLIST_SEED
TEST_YEARS   = 2          # how far back to look for signals
WARMUP_DAYS  = 80         # trading days needed before first signal (for MA50)
TARGET_PCT   = 0.08       # exit at +8%  (midpoint of 5-15% range)
STOP_PCT     = 0.05       # exit at -5%  (midpoint of 3-8% range)
MAX_HOLD     = 20         # max trading days to hold before forced exit
MIN_RR       = 1.5        # minimum reward:risk (same as live config)


# ── data layer ──────────────────────────────────────────────────────────────────

def download_all(symbols: List[str], years: int) -> Dict[str, pd.DataFrame]:
    """Download OHLCV for all symbols in one batch. Returns dict of DataFrames."""
    extra   = ["SPY", "^VIX"]
    all_sym = list(set(symbols) | set(extra))
    print(f"Downloading {len(all_sym)} symbols ({years + 1} years of history)...")

    raw = yf.download(
        all_sym,
        period=f"{years + 1}y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        timeout=60,
    )
    result: Dict[str, pd.DataFrame] = {}
    for sym in all_sym:
        yf_sym = sym  # ^VIX stays as-is
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                df = raw.xs(yf_sym, axis=1, level=1).copy()
            else:
                df = raw.copy()
            df = df.dropna(subset=["Close"]).sort_index()
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            result[sym] = df
        except Exception:
            result[sym] = pd.DataFrame()
    print(f"Downloaded. Date range: {raw.index[0].date()} to {raw.index[-1].date()}\n")
    return result


class BacktestClient:
    """
    Feeds historical data to research_engine functions as if it were
    the live yfinance client — but only shows data up to `as_of`.
    """
    def __init__(self, all_data: Dict[str, pd.DataFrame]):
        self._data   = all_data
        self.as_of   = pd.Timestamp.now()

    def _slice(self, symbol: str, days: int) -> pd.DataFrame:
        df = self._data.get(symbol, pd.DataFrame())
        if df.empty:
            return df
        return df[df.index <= self.as_of].tail(days)

    def get_price_history(self, symbol: str, days: int) -> List[Dict]:
        yf_sym = "^VIX" if symbol == "VIX" else symbol
        df = self._slice(yf_sym, days)
        return [
            {"date": str(ts.date()), "open": r.Open, "high": r.High,
             "low": r.Low, "close": r.Close, "volume": r.Volume}
            for ts, r in df.iterrows()
        ]

    def get_current_price(self, symbol: str) -> float:
        bars = self.get_price_history(symbol, days=3)
        return bars[-1]["close"] if bars else 0.0

    def get_upcoming_earnings(self, symbol: str) -> Optional[str]:
        return None   # skip earnings gate in backtest

    def get_recent_news(self, symbol: str, days: int) -> List[Dict]:
        return []

    def get_vix_data(self, days: int) -> List[Dict]:
        return self.get_price_history("VIX", days=days)


# ── signal detection ────────────────────────────────────────────────────────────

def screen_day(client: BacktestClient, symbol: str) -> Optional[TechnicalSignal]:
    """Run the technical screen for one symbol on client.as_of date."""
    try:
        sig = technical_screen(symbol, client)
        return sig if sig.passes_screen else None
    except Exception:
        return None


def macro_ok(client: BacktestClient) -> bool:
    """True if macro gate is NORMAL or RAISE_BAR (not NO_TRADE)."""
    try:
        snap = macro_sentiment_check(client)
        return snap.state != MacroState.NO_TRADE
    except Exception:
        return True


# ── trade simulation ────────────────────────────────────────────────────────────

def simulate_trade(df: pd.DataFrame, entry_date: pd.Timestamp,
                   entry_price: float) -> Dict:
    """
    Simulate entering at entry_price on entry_date.
    Exits when price first hits +TARGET_PCT or -STOP_PCT,
    or after MAX_HOLD trading days.
    Returns a result dict.
    """
    target = entry_price * (1 + TARGET_PCT)
    stop   = entry_price * (1 - STOP_PCT)
    future = df[df.index > entry_date].head(MAX_HOLD)

    for i, (ts, row) in enumerate(future.iterrows()):
        # Assume worst case: stop checked before target on same day
        if row.Low <= stop:
            return {"outcome": "STOP", "pnl_pct": -STOP_PCT,
                    "hold_days": i + 1, "exit_date": ts}
        if row.High >= target:
            return {"outcome": "TARGET", "pnl_pct": TARGET_PCT,
                    "hold_days": i + 1, "exit_date": ts}

    # Forced exit at close of last bar
    if len(future) == 0:
        return {"outcome": "NO_DATA", "pnl_pct": 0.0, "hold_days": 0, "exit_date": entry_date}
    last   = future.iloc[-1]
    pnl    = (last.Close - entry_price) / entry_price
    return {"outcome": "EXPIRED", "pnl_pct": pnl,
            "hold_days": len(future), "exit_date": future.index[-1]}


# ── main backtest loop ──────────────────────────────────────────────────────────

def run_backtest(data: Dict[str, pd.DataFrame]) -> List[Dict]:
    client   = BacktestClient(data)
    spy_days = data.get("SPY", pd.DataFrame())
    if spy_days.empty:
        print("ERROR: no SPY data")
        return []

    # Trading days in the test window
    test_start = spy_days.index[-1] - pd.DateOffset(years=TEST_YEARS)
    test_days  = spy_days[spy_days.index >= test_start].index

    trades: List[Dict] = []
    signals_per_day = {}

    print(f"Scanning {len(SYMBOLS)} symbols over {len(test_days)} trading days...")
    total = len(test_days)

    for i, day in enumerate(test_days):
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{total} days processed ({len(trades)} signals so far)...")

        client.as_of = day

        # Skip if macro says NO_TRADE
        if not macro_ok(client):
            continue

        signals_per_day[day] = 0
        for sym in SYMBOLS:
            sig = screen_day(client, sym)
            if sig is None:
                continue

            # Check reward:risk using support level
            if sig.support_level is None:
                continue
            entry = sig.current_price
            stop_distance  = (entry - sig.support_level) / entry
            if not (config.STOP_LOSS_PCT_MIN <= stop_distance <= config.STOP_LOSS_PCT_MAX):
                continue
            target_distance = TARGET_PCT
            rr = target_distance / stop_distance
            if rr < MIN_RR:
                continue

            # Simulate: enter at next day's open
            sym_df = data.get(sym, pd.DataFrame())
            future = sym_df[sym_df.index > day]
            if future.empty:
                continue
            next_bar   = future.iloc[0]
            entry_price = next_bar.Open
            entry_date  = future.index[0]

            result = simulate_trade(sym_df, entry_date, entry_price)
            trades.append({
                "symbol":       sym,
                "signal_date":  day.date(),
                "entry_date":   entry_date.date(),
                "entry_price":  round(entry_price, 2),
                "rsi":          round(sig.rsi, 1),
                "rsi_bounce":   sig.rsi_bounce,
                "rsi_momentum": sig.rsi_momentum,
                "vol_confirm":  sig.volume_confirmed,
                **result,
            })
            signals_per_day[day] += 1

    return trades


# ── reporting ───────────────────────────────────────────────────────────────────

def print_report(trades: List[Dict]):
    if not trades:
        print("\nNo signals generated in the test period.")
        print("The screens may be too strict — consider loosening criteria.")
        return

    total   = len(trades)
    wins    = [t for t in trades if t["outcome"] == "TARGET"]
    losses  = [t for t in trades if t["outcome"] == "STOP"]
    expired = [t for t in trades if t["outcome"] == "EXPIRED"]

    win_rate    = len(wins) / total
    avg_win     = TARGET_PCT
    avg_loss    = STOP_PCT
    expectancy  = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    avg_hold    = sum(t["hold_days"] for t in trades) / total

    expired_avg = (sum(t["pnl_pct"] for t in expired) / len(expired)) if expired else 0

    print("\n" + "=" * 60)
    print(f"  BACKTEST RESULTS  ({TEST_YEARS} years, {total} signals)")
    print("=" * 60)
    print(f"  Win rate     : {win_rate:.1%}  ({len(wins)} hits target / {total} total)")
    print(f"  Loss rate    : {len(losses)/total:.1%}  ({len(losses)} hit stop)")
    print(f"  Expired/held : {len(expired)/total:.1%}  ({len(expired)} forced exits, avg {expired_avg:.1%})")
    print(f"  Avg hold     : {avg_hold:.1f} trading days")
    print(f"  Expectancy   : {expectancy:+.2%} per trade")
    print()

    if expectancy > 0:
        print(f"  VERDICT: POSITIVE EXPECTANCY (+{expectancy:.2%}/trade)")
        print(f"  With a win rate of {win_rate:.0%} and {TARGET_PCT:.0%}/{STOP_PCT:.0%}")
        print(f"  reward/risk, the strategy has edge over this period.")
    else:
        print(f"  VERDICT: NEGATIVE EXPECTANCY ({expectancy:.2%}/trade)")
        print(f"  The screens are not generating profitable signals.")
        print(f"  Consider: tighter entry criteria, wider targets, or")
        print(f"  looser stops.")

    print()
    print("  SIGNALS BY SYMBOL:")
    sym_counts = {}
    sym_wins   = {}
    for t in trades:
        s = t["symbol"]
        sym_counts[s] = sym_counts.get(s, 0) + 1
        sym_wins[s]   = sym_wins.get(s, 0) + (1 if t["outcome"] == "TARGET" else 0)
    for sym, count in sorted(sym_counts.items(), key=lambda x: -x[1]):
        wr = sym_wins.get(sym, 0) / count
        print(f"    {sym:6s}  {count:3d} signals  {wr:.0%} win rate")

    print()
    print("  SIGNAL BREAKDOWN:")
    rsi_b = [t for t in trades if t["rsi_bounce"]]
    rsi_m = [t for t in trades if t["rsi_momentum"] and not t["rsi_bounce"]]
    print(f"    RSI bounce signals  : {len(rsi_b):3d}  win rate {sum(1 for t in rsi_b if t['outcome']=='TARGET')/max(len(rsi_b),1):.0%}")
    print(f"    RSI momentum signals: {len(rsi_m):3d}  win rate {sum(1 for t in rsi_m if t['outcome']=='TARGET')/max(len(rsi_m),1):.0%}")

    print("=" * 60)


if __name__ == "__main__":
    data   = download_all(SYMBOLS, TEST_YEARS)
    trades = run_backtest(data)
    print_report(trades)
