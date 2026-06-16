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
    def test_tight_stop_gives_larger_position(self):
        # tight stop → less risk per share → can buy more
        size_tight = calculate_position_size(250.0, stop_pct=0.03)
        size_wide  = calculate_position_size(250.0, stop_pct=0.08)
        assert size_tight > size_wide

    def test_wide_stop_capped_at_minimum(self):
        # very wide stop would produce tiny position — clamp to min
        size = calculate_position_size(250.0, stop_pct=0.08)
        assert size >= 250.0 * config.POSITION_SIZE_PCT_MIN

    def test_tight_stop_capped_at_maximum(self):
        # very tight stop would produce huge position — clamp to max
        size = calculate_position_size(250.0, stop_pct=0.03)
        assert size <= 250.0 * config.POSITION_SIZE_PCT_MAX

    def test_mid_stop_risk_based(self):
        # at stop=5%, risk=1.5% → $3.75/$0.05 = $75 on $250 account
        size = calculate_position_size(250.0, stop_pct=0.05)
        expected = (250.0 * config.RISK_PER_TRADE_PCT) / 0.05
        min_size = 250.0 * config.POSITION_SIZE_PCT_MIN
        max_size = 250.0 * config.POSITION_SIZE_PCT_MAX
        assert size == round(max(min_size, min(max_size, expected)), 2)

    def test_raises_on_non_positive_account(self):
        with pytest.raises(ValueError):
            calculate_position_size(0, stop_pct=0.05)
        with pytest.raises(ValueError):
            calculate_position_size(-100, stop_pct=0.05)

    def test_raises_on_non_positive_stop(self):
        with pytest.raises(ValueError):
            calculate_position_size(250.0, stop_pct=0.0)


class TestCalculateBracketPrices:
    def test_correct_stop_and_target(self):
        bp = calculate_bracket_prices(entry_price=100.0, stop_pct=0.05, target_pct=0.08)
        assert abs(bp.stop_price - 95.0) < 0.001
        assert abs(bp.target_price - 108.0) < 0.001

    def test_stop_at_min_bound(self):
        bp = calculate_bracket_prices(100.0, stop_pct=config.STOP_LOSS_PCT_MIN, target_pct=0.08)
        assert bp.stop_price == pytest.approx(100.0 * (1 - config.STOP_LOSS_PCT_MIN), rel=1e-5)

    def test_stop_at_max_bound(self):
        bp = calculate_bracket_prices(100.0, stop_pct=config.STOP_LOSS_PCT_MAX, target_pct=0.08)
        assert bp.stop_price == pytest.approx(100.0 * (1 - config.STOP_LOSS_PCT_MAX), rel=1e-5)

    def test_target_at_min_bound(self):
        bp = calculate_bracket_prices(100.0, stop_pct=0.05, target_pct=config.PROFIT_TARGET_PCT_MIN)
        assert bp.target_price == pytest.approx(100.0 * (1 + config.PROFIT_TARGET_PCT_MIN), rel=1e-5)

    def test_target_at_max_bound(self):
        bp = calculate_bracket_prices(100.0, stop_pct=0.05, target_pct=config.PROFIT_TARGET_PCT_MAX)
        assert bp.target_price == pytest.approx(100.0 * (1 + config.PROFIT_TARGET_PCT_MAX), rel=1e-5)

    def test_raises_stop_below_min(self):
        with pytest.raises(ValueError, match="stop_pct"):
            calculate_bracket_prices(100.0, stop_pct=0.01, target_pct=0.08)

    def test_raises_stop_above_max(self):
        with pytest.raises(ValueError, match="stop_pct"):
            calculate_bracket_prices(100.0, stop_pct=0.99, target_pct=0.08)

    def test_raises_target_below_min(self):
        with pytest.raises(ValueError, match="target_pct"):
            calculate_bracket_prices(100.0, stop_pct=0.05, target_pct=0.01)

    def test_raises_target_above_max(self):
        with pytest.raises(ValueError, match="target_pct"):
            calculate_bracket_prices(100.0, stop_pct=0.05, target_pct=0.99)

    def test_raises_non_positive_entry(self):
        with pytest.raises(ValueError):
            calculate_bracket_prices(0.0, stop_pct=0.05, target_pct=0.08)

    def test_high_price_precision(self):
        bp = calculate_bracket_prices(entry_price=543.21, stop_pct=0.06, target_pct=0.10)
        expected_stop   = round(543.21 * 0.94, 4)
        expected_target = round(543.21 * 1.10, 4)
        assert bp.stop_price   == expected_stop
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
        assert clamp_target_pct(0.08) == 0.08

    def test_clamp_target_below_min(self):
        assert clamp_target_pct(0.001) == config.PROFIT_TARGET_PCT_MIN

    def test_clamp_target_above_max(self):
        assert clamp_target_pct(0.99) == config.PROFIT_TARGET_PCT_MAX
