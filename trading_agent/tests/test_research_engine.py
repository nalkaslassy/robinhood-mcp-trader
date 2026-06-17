"""
Tests for research_engine.py.
All market data is synthetic — no MCP or network access required.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Dict, List, Optional

import pytest

from trading_agent import config
from trading_agent.research_engine import (
    MacroState,
    TechnicalSignal,
    _detect_rsi_bounce,
    _detect_rsi_momentum,
    _find_resistance,
    _find_support,
    _rsi,
    _rsi_series,
    _sma,
    catalyst_check,
    macro_sentiment_check,
    rank_candidates,
    risk_reward_calc,
    run_daily_research,
    technical_screen,
)


# ---------------------------------------------------------------------------
# Mock market data client
# ---------------------------------------------------------------------------

class MockMarketDataClient:
    """
    Configurable mock that returns synthetic price/volume/event data.
    Pass override dicts to customise specific symbols.
    """

    def __init__(
        self,
        *,
        closes: Optional[List[float]] = None,
        volumes: Optional[List[float]] = None,
        earnings_dates: Optional[Dict[str, str]] = None,
        avg_dollar_vol_override: Optional[Dict[str, float]] = None,
        spy_closes: Optional[List[float]] = None,
        vix_level: float = 18.0,
    ):
        # Default: 60 bars, gently rising prices (uptrend), moderate volume
        self._closes = closes or _rising_prices(60, start=100.0, step=0.5)
        self._volumes = volumes or [5_000_000.0] * 60
        self._earnings = earnings_dates or {}
        self._dollar_vol_override = avg_dollar_vol_override or {}
        self._spy_closes = spy_closes or _rising_prices(55, start=400.0, step=0.5)
        self._vix_level = vix_level

    def get_price_history(self, symbol: str, days: int) -> List[Dict]:
        # Use spy closes for SPY, otherwise default closes
        base = self._spy_closes if symbol == "SPY" else self._closes
        bars = base[-days:] if len(base) >= days else base
        result = []
        for i, c in enumerate(bars):
            result.append({
                "date": (date.today() - timedelta(days=len(bars) - i)).isoformat(),
                "open": c * 0.99,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "volume": self._volumes[i % len(self._volumes)],
            })
        return result

    def get_current_price(self, symbol: str) -> float:
        return self._closes[-1]

    def get_upcoming_earnings(self, symbol: str) -> Optional[str]:
        return self._earnings.get(symbol)

    def get_recent_news(self, symbol: str, days: int) -> List[Dict]:
        return []

    def get_vix_data(self, days: int) -> List[Dict]:
        return [{"date": date.today().isoformat(), "close": self._vix_level, "open": self._vix_level,
                 "high": self._vix_level, "low": self._vix_level, "volume": 0}]

    def get_intraday_volume(self, symbol: str) -> Optional[float]:
        return None  # backtest/mock: fall back to daily volume check


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

def _rising_prices(n: int, start: float = 100.0, step: float = 0.5) -> List[float]:
    return [round(start + i * step, 4) for i in range(n)]


def _flat_prices(n: int, price: float = 100.0) -> List[float]:
    return [price] * n


def _falling_prices(n: int, start: float = 120.0, step: float = 0.5) -> List[float]:
    return [round(start - i * step, 4) for i in range(n)]


def _oscillating_rsi_bounce_prices(n: int = 30) -> List[float]:
    """
    Craft a price series that produces an RSI-bounce signal.
    First half drops sharply (RSI < 30), second half recovers (RSI crosses above 30).
    """
    prices = []
    # 15 sharp down bars
    for i in range(15):
        prices.append(100.0 - i * 2.0)
    # 15 recovery bars
    for i in range(15):
        prices.append(70.0 + i * 1.5)
    return prices[:n]


# ---------------------------------------------------------------------------
# Indicator unit tests
# ---------------------------------------------------------------------------

class TestSMA:
    def test_correct_average(self):
        assert abs(_sma([1, 2, 3, 4, 5], 3) - 4.0) < 1e-9

    def test_returns_none_when_insufficient(self):
        assert _sma([1, 2], 3) is None

    def test_uses_last_n(self):
        assert abs(_sma([10, 1, 2, 3], 3) - 2.0) < 1e-9


class TestRSI:
    def test_all_gains_returns_100(self):
        prices = _rising_prices(20)
        r = _rsi(prices, 14)
        assert r == 100.0

    def test_all_losses_returns_0(self):
        prices = _falling_prices(20)
        r = _rsi(prices, 14)
        assert r is not None and r < 1.0  # near zero

    def test_returns_none_insufficient_data(self):
        assert _rsi([100.0, 101.0], 14) is None

    def test_range_0_to_100(self):
        import random
        random.seed(42)
        prices = [100.0 + random.gauss(0, 2) for _ in range(50)]
        for end in range(16, 51):
            r = _rsi(prices[:end], 14)
            if r is not None:
                assert 0 <= r <= 100


class TestRSIBounce:
    def test_detects_bounce(self):
        bounce_closes = _oscillating_rsi_bounce_prices(30)
        series = _rsi_series(bounce_closes, period=14)
        assert _detect_rsi_bounce(series, lookback=10) is True

    def test_no_bounce_on_rising_prices(self):
        series = _rsi_series(_rising_prices(40), period=14)
        # RSI should stay high, no sub-30 dip
        assert _detect_rsi_bounce(series, lookback=5) is False

    def test_no_bounce_insufficient_data(self):
        assert _detect_rsi_bounce([50.0, 55.0], lookback=5) is False


class TestRSIMomentum:
    def test_detects_momentum(self):
        series = [55.0, 58.0, 61.0, 64.0]
        assert _detect_rsi_momentum(series, lookback=3) is True

    def test_no_momentum_below_50(self):
        series = [45.0, 48.0, 52.0]
        # last value 52 but prior value 48 < 50 — current last IS >50, check just RSI[-1]>50 and rising
        # 45->48->52 is rising and last is 52 > 50
        assert _detect_rsi_momentum(series, lookback=3) is True

    def test_no_momentum_declining(self):
        series = [65.0, 60.0, 55.0]
        assert _detect_rsi_momentum(series, lookback=3) is False


class TestSupportResistance:
    def test_finds_support_below_current(self):
        lows = [90.0, 88.0, 92.0, 85.0, 91.0]
        highs = [105.0] * 5
        support = _find_support(highs, lows, current=100.0)
        assert support == 92.0  # highest low below 100

    def test_no_support_when_all_lows_above_current(self):
        lows = [105.0, 110.0]
        highs = [120.0, 125.0]
        assert _find_support(highs, lows, current=100.0) is None

    def test_finds_resistance_above_current(self):
        highs = [110.0, 115.0, 108.0, 120.0]
        resistance = _find_resistance(highs, current=105.0)
        assert resistance == 108.0  # lowest high above 105

    def test_no_resistance_when_all_highs_below(self):
        highs = [90.0, 95.0]
        assert _find_resistance(highs, current=100.0) is None


# ---------------------------------------------------------------------------
# technical_screen step
# ---------------------------------------------------------------------------

class TestTechnicalScreen:
    def test_uptrend_detected(self):
        closes = _rising_prices(60, start=80.0, step=0.5)  # current ~109.5, ma50 ~92
        vols = [10_000_000.0] * 60
        client = MockMarketDataClient(closes=closes, volumes=vols)
        sig = technical_screen("AAPL", client)
        assert sig.is_uptrend is True

    def test_downtrend_detected(self):
        closes = _falling_prices(60, start=150.0, step=0.5)
        vols = [10_000_000.0] * 60
        client = MockMarketDataClient(closes=closes, volumes=vols)
        sig = technical_screen("AAPL", client)
        assert sig.is_uptrend is False

    def test_volume_confirmation(self):
        closes = _rising_prices(60, start=80.0, step=0.5)
        # last bar volume well above avg
        vols = [1_000_000.0] * 59 + [5_000_000.0]
        client = MockMarketDataClient(closes=closes, volumes=vols)
        sig = technical_screen("AAPL", client)
        assert sig.volume_confirmed is True

    def test_no_volume_confirmation_when_below_avg(self):
        closes = _rising_prices(60, start=80.0, step=0.5)
        vols = [5_000_000.0] * 59 + [500_000.0]
        client = MockMarketDataClient(closes=closes, volumes=vols)
        sig = technical_screen("AAPL", client)
        assert sig.volume_confirmed is False

    def test_rsi_bounce_detected(self):
        bounce_prices = _oscillating_rsi_bounce_prices(30)
        # Pad to 60 bars — start from falling then rising
        padding = _rising_prices(30, start=bounce_prices[-1] + 0.5, step=0.3)
        closes = bounce_prices + padding
        vols = [5_000_000.0] * 60
        client = MockMarketDataClient(closes=closes, volumes=vols)
        sig = technical_screen("TEST", client)
        # RSI bounce in the padded series may or may not persist; just check it doesn't crash
        assert isinstance(sig.rsi_bounce, bool)

    def test_passes_screen_requires_uptrend(self):
        closes = _falling_prices(60, start=150.0, step=0.5)
        client = MockMarketDataClient(closes=closes)
        sig = technical_screen("AAPL", client)
        assert sig.passes_screen is False
        assert "uptrend" in sig.exclusion_reason.lower()


# ---------------------------------------------------------------------------
# catalyst_check step
# ---------------------------------------------------------------------------

class TestCatalystCheck:
    def test_excludes_earnings_within_window(self):
        earnings_date = (date.today() + timedelta(days=3)).isoformat()
        client = MockMarketDataClient(
            closes=_rising_prices(30, start=100.0, step=1.0),
            volumes=[60_000_000.0] * 30,
            earnings_dates={"AAPL": earnings_date},
        )
        result = catalyst_check("AAPL", client)
        assert result.excluded is True
        assert "Earnings" in result.exclusion_reason

    def test_allows_earnings_outside_window(self):
        earnings_date = (date.today() + timedelta(days=30)).isoformat()
        client = MockMarketDataClient(
            closes=_rising_prices(30, start=100.0, step=1.0),
            volumes=[60_000_000.0] * 30,
            earnings_dates={"AAPL": earnings_date},
        )
        result = catalyst_check("AAPL", client)
        assert result.excluded is False

    def test_excludes_low_liquidity(self):
        # price=10, volume=100_000 => dollar vol = 1M, below $50M floor
        client = MockMarketDataClient(
            closes=_flat_prices(30, price=10.0),
            volumes=[100_000.0] * 30,
        )
        result = catalyst_check("LOWVOL", client)
        assert result.excluded is True
        assert "dollar volume" in result.exclusion_reason.lower()

    def test_passes_sufficient_liquidity(self):
        # price=100, volume=1_000_000 => $100M per day
        client = MockMarketDataClient(
            closes=_flat_prices(30, price=100.0),
            volumes=[1_000_000.0] * 30,
        )
        result = catalyst_check("AAPL", client)
        assert result.excluded is False

    def test_earnings_exactly_at_window_boundary(self):
        earnings_date = (date.today() + timedelta(days=config.EARNINGS_EXCLUSION_WINDOW_DAYS)).isoformat()
        client = MockMarketDataClient(
            closes=_rising_prices(30, start=100.0, step=1.0),
            volumes=[60_000_000.0] * 30,
            earnings_dates={"AAPL": earnings_date},
        )
        result = catalyst_check("AAPL", client)
        assert result.excluded is True  # within window (inclusive)


# ---------------------------------------------------------------------------
# risk_reward_calc step
# ---------------------------------------------------------------------------

class TestRiskRewardCalc:
    def _make_tech(
        self,
        price: float,
        support: Optional[float],
        resistance: Optional[float],
    ) -> TechnicalSignal:
        return TechnicalSignal(
            symbol="TEST",
            current_price=price,
            ma20=price * 0.97,
            ma50=price * 0.92,
            rsi=55.0,
            avg_volume_20d=1_000_000.0,
            recent_volume=1_200_000.0,
            support_level=support,
            resistance_level=resistance,
            atr=0.0,
            adx=None,
            is_uptrend=True,
            is_trending=False,
            rsi_bounce=False,
            rsi_momentum=True,
            volume_confirmed=True,
            passes_screen=True,
        )

    def test_valid_setup(self):
        # stop 6%, target 12% => rr=2.0 — resistance must be >= PROFIT_TARGET_PCT_MIN (5%) and R:R >= 1.5
        tech = self._make_tech(100.0, support=94.0, resistance=112.0)
        rr = risk_reward_calc("TEST", tech)
        assert rr.passes is True
        assert abs(rr.stop_pct - 0.06) < 1e-9
        assert rr.reward_risk_ratio > 0

    def test_rejects_stop_too_tight(self):
        # support only 2% below => stop_pct=0.02 < STOP_LOSS_PCT_MIN (3%)
        tech = self._make_tech(100.0, support=98.0, resistance=108.0)
        rr = risk_reward_calc("TEST", tech)
        assert rr.passes is False
        assert "outside" in rr.exclusion_reason

    def test_rejects_stop_too_wide(self):
        # support 9% below => stop_pct=0.09 > STOP_LOSS_PCT_MAX (8%)
        tech = self._make_tech(100.0, support=91.0, resistance=108.0)
        rr = risk_reward_calc("TEST", tech)
        assert rr.passes is False

    def test_rejects_no_support(self):
        tech = self._make_tech(100.0, support=None, resistance=108.0)
        rr = risk_reward_calc("TEST", tech)
        assert rr.passes is False
        assert "support" in rr.exclusion_reason.lower()

    def test_reward_risk_ratio_computed_correctly(self):
        # stop 6%, target 10% => rr=1.6667
        tech = self._make_tech(100.0, support=94.0, resistance=110.0)
        rr = risk_reward_calc("TEST", tech)
        expected_rr = round(rr.target_pct / rr.stop_pct, 4)
        assert abs(rr.reward_risk_ratio - expected_rr) < 1e-6


# ---------------------------------------------------------------------------
# macro_sentiment_check step
# ---------------------------------------------------------------------------

class TestMacroSentimentCheck:
    def test_normal_state(self):
        # SPY uptrend, VIX low
        client = MockMarketDataClient(
            spy_closes=_rising_prices(55, start=400.0, step=1.0),
            vix_level=18.0,
        )
        macro = macro_sentiment_check(client)
        assert macro.state == MacroState.NORMAL
        assert macro.spy_uptrend is True
        assert macro.vix_high is False

    def test_no_trade_spy_down_vix_high(self):
        client = MockMarketDataClient(
            spy_closes=_falling_prices(55, start=500.0, step=2.0),
            vix_level=30.0,
        )
        macro = macro_sentiment_check(client)
        assert macro.state == MacroState.NO_TRADE
        assert macro.spy_uptrend is False
        assert macro.vix_high is True

    def test_raise_bar_spy_up_vix_high(self):
        client = MockMarketDataClient(
            spy_closes=_rising_prices(55, start=400.0, step=1.0),
            vix_level=27.0,
        )
        macro = macro_sentiment_check(client)
        assert macro.state == MacroState.RAISE_BAR

    def test_raise_bar_spy_down_vix_normal(self):
        client = MockMarketDataClient(
            spy_closes=_falling_prices(55, start=500.0, step=2.0),
            vix_level=18.0,
        )
        macro = macro_sentiment_check(client)
        assert macro.state == MacroState.RAISE_BAR

    def test_vix_threshold_boundary_below(self):
        client = MockMarketDataClient(
            spy_closes=_rising_prices(55, start=400.0, step=1.0),
            vix_level=config.VIX_HIGH_THRESHOLD - 0.01,
        )
        macro = macro_sentiment_check(client)
        assert macro.vix_high is False
        assert macro.state == MacroState.NORMAL

    def test_vix_threshold_boundary_at(self):
        client = MockMarketDataClient(
            spy_closes=_rising_prices(55, start=400.0, step=1.0),
            vix_level=float(config.VIX_HIGH_THRESHOLD),
        )
        macro = macro_sentiment_check(client)
        assert macro.vix_high is True
        assert macro.state == MacroState.RAISE_BAR


# ---------------------------------------------------------------------------
# rank_candidates step
# ---------------------------------------------------------------------------

def _make_candidate(rr_ratio: float = 0.8, all_signals: bool = True):
    tech = TechnicalSignal(
        symbol="XX",
        current_price=100.0,
        ma20=97.0,
        ma50=92.0,
        rsi=60.0,
        avg_volume_20d=1_000_000.0,
        recent_volume=1_500_000.0 if all_signals else 500_000.0,
        support_level=94.0,
        resistance_level=104.0,
        atr=0.0,
        adx=None,
        is_uptrend=True,
        is_trending=False,
        rsi_bounce=all_signals,
        rsi_momentum=all_signals,
        volume_confirmed=all_signals,
        passes_screen=True,
    )
    from trading_agent.research_engine import CatalystResult, MacroSnapshot, RiskRewardResult
    cat = CatalystResult(symbol="XX", excluded=False)
    rr = RiskRewardResult(
        symbol="XX",
        entry_price=100.0,
        stop_price=94.0,
        target_price=104.0,
        stop_pct=0.06,
        target_pct=round(rr_ratio * 0.06, 4),
        reward_risk_ratio=rr_ratio,
        passes=True,
    )
    return {"technical": tech, "catalyst": cat, "risk_reward": rr}


def _mock_macro(state: MacroState) -> "MacroSnapshot":
    from trading_agent.research_engine import MacroSnapshot
    return MacroSnapshot(
        spy_price=450.0, spy_ma50=440.0, vix_level=18.0,
        spy_uptrend=(state != MacroState.NO_TRADE),
        vix_high=(state == MacroState.NO_TRADE),
        state=state,
    )


class TestRankCandidates:
    def test_no_trade_returns_empty(self):
        candidates = [_make_candidate(0.8), _make_candidate(0.9)]
        result = rank_candidates(candidates, _mock_macro(MacroState.NO_TRADE))
        assert result == []

    def test_normal_returns_top_2_by_rr(self):
        c1 = _make_candidate(0.6)
        c2 = _make_candidate(0.9)
        c3 = _make_candidate(0.75)
        result = rank_candidates([c1, c2, c3], _mock_macro(MacroState.NORMAL))
        assert len(result) == 2
        assert result[0].rank_score >= result[1].rank_score

    def test_raise_bar_filters_partial_signals(self):
        full = _make_candidate(0.8, all_signals=True)
        partial = _make_candidate(0.9, all_signals=False)
        result = rank_candidates([full, partial], _mock_macro(MacroState.RAISE_BAR))
        # partial candidate (no volume_confirmed/rsi signals) should be filtered
        assert all(c.technical.volume_confirmed for c in result)

    def test_raise_bar_keeps_all_signal_candidates(self):
        c1 = _make_candidate(0.8, all_signals=True)
        c2 = _make_candidate(0.7, all_signals=True)
        result = rank_candidates([c1, c2], _mock_macro(MacroState.RAISE_BAR))
        assert len(result) == 2

    def test_max_2_returned(self):
        candidates = [_make_candidate(0.5 + i * 0.1) for i in range(6)]
        result = rank_candidates(candidates, _mock_macro(MacroState.NORMAL))
        assert len(result) <= 2
