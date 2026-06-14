"""Tests for position_sizing.py."""
import pytest
from trading_agent.position_sizing import (
    calculate_position_size,
    calculate_bracket_prices,
    shares_from_dollar_amount,
    clamp_stop_pct,
    clamp_target_pct,
)
from trading_agent import config


class TestCalculatePositionSize:
    @pytest.mark.parametrize("account_value,expected_low,expected_high", [
        (250.00, 62.50, 75.00),
        (400.00, 100.00, 120.00),
        (700.00, 175.00, 210.00),
        (1200.00, 300.00, 360.00),
    ])
    def test_correct_range(self, account_value, expected_low, expected_high):
        low, high = calculate_position_size(account_value)
        assert abs(low - expected_low) < 0.01
        assert abs(high - expected_high) < 0.01

    def test_always_within_pct_bounds(self):
        for val in [250, 400, 700, 1200, 2500, 10000]:
            low, high = calculate_position_size(float(val))
            assert abs(low / val - config.POSITION_SIZE_PCT_MIN) < 1e-9
            assert abs(high / val - config.POSITION_SIZE_PCT_MAX) < 1e-9

    def test_raises_on_non_positive(self):
        with pytest.raises(ValueError):
            calculate_position_size(0)
        with pytest.raises(ValueError):
            calculate_position_size(-100)

    def test_very_small_account(self):
        low, high = calculate_position_size(1.00)
        assert low == round(1.00 * config.POSITION_SIZE_PCT_MIN, 2)
        assert high == round(1.00 * config.POSITION_SIZE_PCT_MAX, 2)


class TestCalculateBracketPrices:
    def test_correct_stop_and_target(self):
        bp = calculate_bracket_prices(entry_price=100.0, stop_pct=0.05, target_pct=0.03)
        assert abs(bp.stop_price - 95.0) < 0.001
        assert abs(bp.target_price - 103.0) < 0.001

    def test_stop_at_min_bound(self):
        bp = calculate_bracket_prices(100.0, stop_pct=config.STOP_LOSS_PCT_MIN, target_pct=0.03)
        assert bp.stop_price == pytest.approx(100.0 * (1 - config.STOP_LOSS_PCT_MIN), rel=1e-5)

    def test_stop_at_max_bound(self):
        bp = calculate_bracket_prices(100.0, stop_pct=config.STOP_LOSS_PCT_MAX, target_pct=0.03)
        assert bp.stop_price == pytest.approx(100.0 * (1 - config.STOP_LOSS_PCT_MAX), rel=1e-5)

    def test_target_at_min_bound(self):
        bp = calculate_bracket_prices(100.0, stop_pct=0.05, target_pct=config.PROFIT_TARGET_PCT_MIN)
        assert bp.target_price == pytest.approx(100.0 * (1 + config.PROFIT_TARGET_PCT_MIN), rel=1e-5)

    def test_target_at_max_bound(self):
        bp = calculate_bracket_prices(100.0, stop_pct=0.05, target_pct=config.PROFIT_TARGET_PCT_MAX)
        assert bp.target_price == pytest.approx(100.0 * (1 + config.PROFIT_TARGET_PCT_MAX), rel=1e-5)

    def test_raises_stop_below_min(self):
        with pytest.raises(ValueError, match="stop_pct"):
            calculate_bracket_prices(100.0, stop_pct=0.04, target_pct=0.03)

    def test_raises_stop_above_max(self):
        with pytest.raises(ValueError, match="stop_pct"):
            calculate_bracket_prices(100.0, stop_pct=0.08, target_pct=0.03)

    def test_raises_target_below_min(self):
        with pytest.raises(ValueError, match="target_pct"):
            calculate_bracket_prices(100.0, stop_pct=0.05, target_pct=0.01)

    def test_raises_target_above_max(self):
        with pytest.raises(ValueError, match="target_pct"):
            calculate_bracket_prices(100.0, stop_pct=0.05, target_pct=0.05)

    def test_raises_non_positive_entry(self):
        with pytest.raises(ValueError):
            calculate_bracket_prices(0.0, stop_pct=0.05, target_pct=0.03)

    def test_high_price_precision(self):
        bp = calculate_bracket_prices(entry_price=543.21, stop_pct=0.06, target_pct=0.04)
        expected_stop = round(543.21 * 0.94, 4)
        expected_target = round(543.21 * 1.04, 4)
        assert bp.stop_price == expected_stop
        assert bp.target_price == expected_target


class TestSharesFromDollarAmount:
    def test_whole_shares(self):
        qty = shares_from_dollar_amount(100.0, 50.0)
        assert abs(qty - 2.0) < 1e-6

    def test_fractional_shares(self):
        qty = shares_from_dollar_amount(75.0, 100.0)
        assert abs(qty - 0.75) < 1e-6

    def test_raises_non_positive_price(self):
        with pytest.raises(ValueError):
            shares_from_dollar_amount(100.0, 0.0)

    def test_raises_non_positive_amount(self):
        with pytest.raises(ValueError):
            shares_from_dollar_amount(0.0, 100.0)


class TestClampFunctions:
    def test_clamp_stop_within_range(self):
        assert clamp_stop_pct(0.06) == 0.06

    def test_clamp_stop_below_min(self):
        assert clamp_stop_pct(0.01) == config.STOP_LOSS_PCT_MIN

    def test_clamp_stop_above_max(self):
        assert clamp_stop_pct(0.99) == config.STOP_LOSS_PCT_MAX

    def test_clamp_target_within_range(self):
        assert clamp_target_pct(0.03) == 0.03

    def test_clamp_target_below_min(self):
        assert clamp_target_pct(0.001) == config.PROFIT_TARGET_PCT_MIN

    def test_clamp_target_above_max(self):
        assert clamp_target_pct(0.99) == config.PROFIT_TARGET_PCT_MAX
