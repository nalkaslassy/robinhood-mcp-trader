"""Tests for account_state.py using a mock MCP client."""
import pytest
from trading_agent.account_state import AccountStateManager, AccountSnapshot, Position
from trading_agent import config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(cash: float, positions=None, peak_override=None) -> AccountStateManager:
    """Return a seeded AccountStateManager without a real MCP client."""
    mgr = AccountStateManager(mcp_client=None)
    pos_list = positions or []
    snap = AccountSnapshot(
        cash=cash,
        positions=pos_list,
        peak_account_value=peak_override if peak_override is not None else cash + sum(p.market_value for p in pos_list),
    )
    mgr._snapshot = snap
    mgr._peak_value = snap.peak_account_value
    return mgr


def _make_position(symbol="AAPL", qty=1.0, avg_price=100.0, cur_price=100.0, leveraged=False):
    return Position(
        symbol=symbol,
        quantity=qty,
        average_buy_price=avg_price,
        current_price=cur_price,
        is_leveraged_etf=leveraged,
    )


# ---------------------------------------------------------------------------
# Drawdown breaker
# ---------------------------------------------------------------------------

class TestDrawdownBreaker:
    def test_no_drawdown(self):
        mgr = _make_manager(cash=250.0, peak_override=250.0)
        assert mgr.is_drawdown_breaker_active() is False

    def test_drawdown_below_threshold(self):
        # 14.9% drawdown — should NOT trigger
        mgr = _make_manager(cash=212.6, peak_override=250.0)
        snap = mgr.get_snapshot()
        assert snap.drawdown_pct < config.ACCOUNT_DRAWDOWN_BREAKER_PCT
        assert mgr.is_drawdown_breaker_active() is False

    def test_drawdown_exactly_at_threshold(self):
        # exactly 15% drawdown
        mgr = _make_manager(cash=212.5, peak_override=250.0)
        snap = mgr.get_snapshot()
        assert abs(snap.drawdown_pct - 0.15) < 1e-9
        assert mgr.is_drawdown_breaker_active() is True

    def test_drawdown_above_threshold(self):
        # 20% drawdown
        mgr = _make_manager(cash=200.0, peak_override=250.0)
        assert mgr.is_drawdown_breaker_active() is True

    def test_account_with_positions_drawdown(self):
        pos = _make_position(cur_price=80.0, avg_price=100.0, qty=1.0)
        # cash=170, position_value=80, total=250, peak=295 => drawdown ~15.25%
        mgr = _make_manager(cash=170.0, positions=[pos], peak_override=295.0)
        assert mgr.is_drawdown_breaker_active() is True


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

class TestPositionSizing:
    @pytest.mark.parametrize("account_value,expected_low,expected_high", [
        (250.00, 62.50, 75.00),
        (400.00, 100.00, 120.00),
        (700.00, 175.00, 210.00),
        (1200.00, 300.00, 360.00),
    ])
    def test_position_size_range(self, account_value, expected_low, expected_high):
        mgr = _make_manager(cash=account_value)
        low, high = mgr.position_size_range()
        assert abs(low - expected_low) < 0.01
        assert abs(high - expected_high) < 0.01

    def test_range_pct_bounds(self):
        for val in [250, 400, 700, 1200]:
            mgr = _make_manager(cash=float(val))
            low, high = mgr.position_size_range()
            assert abs(low / val - config.POSITION_SIZE_PCT_MIN) < 1e-9
            assert abs(high / val - config.POSITION_SIZE_PCT_MAX) < 1e-9


# ---------------------------------------------------------------------------
# can_open_new_position
# ---------------------------------------------------------------------------

class TestCanOpenNewPosition:
    def test_allows_when_healthy(self):
        mgr = _make_manager(cash=250.0)
        ok, reason = mgr.can_open_new_position()
        assert ok is True
        assert reason == "OK"

    def test_blocks_drawdown_breaker(self):
        mgr = _make_manager(cash=200.0, peak_override=250.0)
        ok, reason = mgr.can_open_new_position()
        assert ok is False
        assert "drawdown" in reason.lower()

    def test_blocks_max_positions(self):
        pos1 = _make_position("AAPL", cur_price=100.0, qty=1.0)
        pos2 = _make_position("MSFT", cur_price=100.0, qty=1.0)
        mgr = _make_manager(cash=500.0, positions=[pos1, pos2])
        ok, reason = mgr.can_open_new_position()
        assert ok is False
        assert "max concurrent" in reason.lower()

    def test_blocks_max_leveraged_etfs(self):
        pos_lev = _make_position("SOXL", cur_price=50.0, qty=1.0, leveraged=True)
        mgr = _make_manager(cash=500.0, positions=[pos_lev])
        ok, reason = mgr.can_open_new_position(symbol="TQQQ", is_leveraged_etf=True)
        assert ok is False
        assert "leveraged" in reason.lower()

    def test_allows_non_leveraged_when_one_leveraged_open(self):
        pos_lev = _make_position("SOXL", cur_price=50.0, qty=1.0, leveraged=True)
        mgr = _make_manager(cash=500.0, positions=[pos_lev])
        ok, reason = mgr.can_open_new_position(symbol="AAPL", is_leveraged_etf=False)
        assert ok is True

    def test_blocks_insufficient_cash_reserve(self):
        # total_value=250, reserve=37.5, min_position=62.5, required=100 — give only 90
        mgr = _make_manager(cash=90.0, peak_override=250.0)
        # manually set total to 250 by adding a position worth 160
        pos = _make_position(cur_price=160.0, qty=1.0)
        snap = mgr.get_snapshot()
        snap.cash = 90.0
        snap.positions = [pos]
        snap.peak_account_value = 250.0
        mgr._peak_value = 250.0
        ok, reason = mgr.can_open_new_position()
        assert ok is False
        assert "cash" in reason.lower()
