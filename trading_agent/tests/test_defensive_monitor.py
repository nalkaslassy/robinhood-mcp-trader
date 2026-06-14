"""Tests for defensive_monitor.py."""
import pytest

from trading_agent import config
from trading_agent.defensive_monitor import DefensiveMonitor, IntradayBar


@pytest.fixture
def monitor():
    return DefensiveMonitor()


class TestVIXSpike:
    def test_no_spike_when_flat(self, monitor):
        assert monitor.check_vix_spike(18.0, 18.0) is False

    def test_no_spike_when_below_threshold(self, monitor):
        # just under 20% spike
        vix_open = 18.0
        vix_now = vix_open * (1 + config.VIX_INTRADAY_SPIKE_PCT - 0.001)
        assert monitor.check_vix_spike(vix_open, vix_now) is False

    def test_spike_exactly_at_threshold(self, monitor):
        vix_open = 18.0
        vix_now = vix_open * (1 + config.VIX_INTRADAY_SPIKE_PCT)
        assert monitor.check_vix_spike(vix_open, vix_now) is True

    def test_spike_above_threshold(self, monitor):
        vix_open = 18.0
        vix_now = vix_open * 1.30
        assert monitor.check_vix_spike(vix_open, vix_now) is True

    def test_vix_drop_does_not_trigger(self, monitor):
        # VIX falling is not a risk signal
        assert monitor.check_vix_spike(25.0, 18.0) is False

    def test_zero_open_safe(self, monitor):
        assert monitor.check_vix_spike(0.0, 20.0) is False


class TestPositionSwings:
    def _bar(self, symbol, open_p, current_p):
        return IntradayBar(
            symbol=symbol,
            open_price=open_p,
            current_price=current_p,
            high=max(open_p, current_p),
            low=min(open_p, current_p),
        )

    def test_no_swing_flags(self, monitor):
        bar = self._bar("AAPL", 100.0, 101.0)  # 1% swing — below 10%
        result = monitor.check_position_swings([bar])
        assert result == []

    def test_swing_exactly_at_threshold(self, monitor):
        pct = config.POSITION_INTRADAY_SWING_PCT
        bar = self._bar("NVDA", 100.0, 100.0 * (1 + pct))
        result = monitor.check_position_swings([bar])
        assert "NVDA" in result

    def test_swing_above_threshold(self, monitor):
        bar = self._bar("TSLA", 100.0, 115.0)  # 15% swing
        result = monitor.check_position_swings([bar])
        assert "TSLA" in result

    def test_negative_swing_also_detected(self, monitor):
        # 12% drop — absolute swing
        bar = self._bar("AMD", 100.0, 88.0)
        result = monitor.check_position_swings([bar])
        assert "AMD" in result

    def test_multiple_symbols_partial_trigger(self, monitor):
        bars = [
            self._bar("AAPL", 100.0, 101.0),   # 1% — safe
            self._bar("TSLA", 100.0, 115.0),   # 15% — flag
        ]
        result = monitor.check_position_swings(bars)
        assert result == ["TSLA"]


class TestShouldPauseNewEntries:
    def _bar(self, symbol, open_p, current_p):
        return IntradayBar(
            symbol=symbol, open_price=open_p, current_price=current_p,
            high=max(open_p, current_p), low=min(open_p, current_p),
        )

    def test_no_triggers_returns_false(self, monitor):
        pause, reason = monitor.should_pause_new_entries(18.0, 18.5)
        assert pause is False
        assert "No defensive" in reason

    def test_vix_spike_alone_triggers_pause(self, monitor):
        vix_now = 18.0 * (1 + config.VIX_INTRADAY_SPIKE_PCT)
        pause, reason = monitor.should_pause_new_entries(18.0, vix_now)
        assert pause is True
        assert "VIX" in reason

    def test_position_swing_alone_triggers_pause(self, monitor):
        bar = self._bar("TSLA", 100.0, 115.0)
        pause, reason = monitor.should_pause_new_entries(18.0, 18.5, [bar])
        assert pause is True
        assert "TSLA" in reason

    def test_both_triggers_combined(self, monitor):
        vix_now = 18.0 * 1.25
        bar = self._bar("NVDA", 100.0, 115.0)
        pause, reason = monitor.should_pause_new_entries(18.0, vix_now, [bar])
        assert pause is True
        assert "VIX" in reason
        assert "NVDA" in reason

    def test_empty_positions_list_safe(self, monitor):
        pause, reason = monitor.should_pause_new_entries(18.0, 18.0, [])
        assert pause is False
