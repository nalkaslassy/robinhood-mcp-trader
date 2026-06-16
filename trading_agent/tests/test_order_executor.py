"""
Tests for order_executor.py — all MCP calls are mocked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from trading_agent import config
from trading_agent.account_state import Position
from trading_agent.order_executor import BracketIntegrityResult, OrderExecutor, OrderResult
from trading_agent.research_engine import (
    CatalystResult,
    MacroSnapshot,
    MacroState,
    RankedCandidate,
    RiskRewardResult,
    TechnicalSignal,
)
from trading_agent.trade_proposal import TradeProposal, create_proposal


# ---------------------------------------------------------------------------
# Mock MCP client
# ---------------------------------------------------------------------------

def _mock_mcp(current_price: float = 100.0, positions=None, open_orders=None):
    client = MagicMock()
    client.get_current_price.return_value = current_price
    client.get_positions.return_value = positions or []
    client.get_open_orders.return_value = open_orders or []
    client.place_limit_buy.return_value = {"id": "entry-001"}
    client.place_stop_loss.return_value = {"id": "stop-001"}
    client.place_limit_sell.return_value = {"id": "target-001"}
    client.cancel_order.return_value = {"status": "cancelled"}
    return client


# ---------------------------------------------------------------------------
# Proposal factory helper
# ---------------------------------------------------------------------------

def _make_proposal(
    symbol="AAPL",
    price=100.0,
    stop_pct=0.06,
    target_pct=0.03,
    position_dollars=62.50,
    created_at=None,
    wash_sale=False,
) -> TradeProposal:
    if created_at is None:
        created_at = datetime.now(tz=timezone.utc)
    tech = TechnicalSignal(
        symbol=symbol, current_price=price, ma20=price*0.97, ma50=price*0.92,
        rsi=60.0, avg_volume_20d=1e6, recent_volume=1.5e6,
        support_level=price*(1-stop_pct), resistance_level=price*(1+target_pct),
        atr=0.0, adx=None,
        is_uptrend=True, is_trending=False, rsi_bounce=True, rsi_momentum=True,
        volume_confirmed=True, passes_screen=True,
    )
    rr = RiskRewardResult(
        symbol=symbol, entry_price=price,
        stop_price=round(price*(1-stop_pct), 4),
        target_price=round(price*(1+target_pct), 4),
        stop_pct=stop_pct, target_pct=target_pct,
        reward_risk_ratio=round(target_pct/stop_pct, 4), passes=True,
    )
    macro = MacroSnapshot(450.0, 440.0, 18.0, True, False, MacroState.NORMAL)
    candidate = RankedCandidate(
        symbol=symbol, technical=tech,
        catalyst=CatalystResult(symbol=symbol, excluded=False),
        risk_reward=rr, macro=macro, rank_score=rr.reward_risk_ratio,
        wash_sale_flag=wash_sale,
    )
    return create_proposal(candidate, position_dollars, created_at=created_at)


# ---------------------------------------------------------------------------
# place_bracket_order tests
# ---------------------------------------------------------------------------

class TestPlaceBracketOrder:
    def test_dry_run_succeeds_without_calling_mcp(self):
        client = _mock_mcp(current_price=100.0)
        executor = OrderExecutor(mcp_client=client)
        executor._dry_run = True
        proposal = _make_proposal(price=100.0)

        result = executor.place_bracket_order(proposal)

        assert result.success is True
        assert result.dry_run is True
        client.place_limit_buy.assert_not_called()
        client.place_stop_loss.assert_not_called()
        client.place_limit_sell.assert_not_called()

    def test_expired_proposal_rejected(self):
        client = _mock_mcp(current_price=100.0)
        executor = OrderExecutor(mcp_client=client)
        executor._dry_run = False
        past = datetime.now(tz=timezone.utc) - timedelta(hours=10)
        proposal = _make_proposal(created_at=past)

        result = executor.place_bracket_order(proposal)

        assert result.success is False
        assert "expired" in result.message.lower()
        client.place_limit_buy.assert_not_called()

    def test_price_outside_range_rejected(self):
        client = _mock_mcp(current_price=110.0)  # entry range ~99.5-100.5
        executor = OrderExecutor(mcp_client=client)
        executor._dry_run = False
        proposal = _make_proposal(price=100.0)

        result = executor.place_bracket_order(proposal)

        assert result.success is False
        assert "outside" in result.message.lower() or "range" in result.message.lower()
        client.place_limit_buy.assert_not_called()

    def test_live_order_calls_all_three_mcp_methods(self):
        client = _mock_mcp(current_price=100.0)
        executor = OrderExecutor(mcp_client=client)
        executor._dry_run = False
        proposal = _make_proposal(price=100.0)

        result = executor.place_bracket_order(proposal)

        assert result.success is True
        client.place_limit_buy.assert_called_once()
        client.place_stop_loss.assert_called_once()
        client.place_limit_sell.assert_called_once()
        assert result.order_id == "entry-001"
        assert result.stop_order_id == "stop-001"
        assert result.target_order_id == "target-001"

    def test_live_order_correct_symbol_passed(self):
        client = _mock_mcp(current_price=200.0)
        executor = OrderExecutor(mcp_client=client)
        executor._dry_run = False
        proposal = _make_proposal(symbol="NVDA", price=200.0)

        executor.place_bracket_order(proposal)

        call_args = client.place_limit_buy.call_args
        assert call_args[0][0] == "NVDA"

    def test_mcp_exception_returns_failure(self):
        client = _mock_mcp(current_price=100.0)
        client.place_limit_buy.side_effect = RuntimeError("Network error")
        executor = OrderExecutor(mcp_client=client)
        executor._dry_run = False
        proposal = _make_proposal(price=100.0)

        result = executor.place_bracket_order(proposal)

        assert result.success is False
        assert "failed" in result.message.lower()


# ---------------------------------------------------------------------------
# check_bracket_integrity tests
# ---------------------------------------------------------------------------

def _position(symbol="AAPL", avg_price=100.0, cur_price=94.0, qty=1.0):
    return Position(
        symbol=symbol, quantity=qty,
        average_buy_price=avg_price, current_price=cur_price,
        is_leveraged_etf=False,
    )


class TestBracketIntegrity:
    def test_both_orders_present_no_emergency(self):
        client = _mock_mcp()
        executor = OrderExecutor(mcp_client=client)
        pos = _position(cur_price=98.0)  # -2% — fine
        orders = [
            {"symbol": "AAPL", "type": "stop_loss", "price": 94.0},
            {"symbol": "AAPL", "type": "limit_sell", "price": 103.0},
        ]
        result = executor.check_bracket_integrity(pos, orders)
        assert result.emergency is False
        assert result.stop_order_active is True
        assert result.target_order_active is True

    def test_emergency_when_beyond_stop_and_no_stop_order(self):
        client = _mock_mcp()
        executor = OrderExecutor(mcp_client=client)
        # Position down 8% (> STOP_LOSS_PCT_MAX=7%) with no stop order
        pos = _position(avg_price=100.0, cur_price=92.0)
        assert pos.unrealized_pnl_pct == pytest.approx(-0.08)
        result = executor.check_bracket_integrity(pos, [])
        assert result.emergency is True
        assert "EMERGENCY" in result.emergency_reason

    def test_no_emergency_when_beyond_stop_but_stop_order_present(self):
        client = _mock_mcp()
        executor = OrderExecutor(mcp_client=client)
        pos = _position(avg_price=100.0, cur_price=91.0)
        orders = [{"symbol": "AAPL", "type": "stop_loss", "price": 93.0}]
        result = executor.check_bracket_integrity(pos, orders)
        assert result.emergency is False

    def test_missing_target_order_flagged(self):
        client = _mock_mcp()
        executor = OrderExecutor(mcp_client=client)
        pos = _position(cur_price=99.0)  # -1% — fine
        orders = [{"symbol": "AAPL", "type": "stop_loss", "price": 93.0}]
        result = executor.check_bracket_integrity(pos, orders)
        assert result.stop_order_active is True
        assert result.target_order_active is False

    def test_unrelated_symbol_orders_ignored(self):
        client = _mock_mcp()
        executor = OrderExecutor(mcp_client=client)
        pos = _position(symbol="AAPL", avg_price=100.0, cur_price=92.0)
        orders = [{"symbol": "MSFT", "type": "stop_loss", "price": 90.0}]
        result = executor.check_bracket_integrity(pos, orders)
        assert result.emergency is True  # AAPL still has no stop


# ---------------------------------------------------------------------------
# cancel_proposal_orders tests
# ---------------------------------------------------------------------------

class TestCancelProposalOrders:
    def test_cancels_stray_orders_for_symbol(self):
        open_orders = [
            {"symbol": "AAPL", "id": "order-123", "type": "limit"},
            {"symbol": "MSFT", "id": "order-456", "type": "limit"},
        ]
        client = _mock_mcp(open_orders=open_orders)
        executor = OrderExecutor(mcp_client=client)
        executor._dry_run = False
        proposal = _make_proposal(symbol="AAPL")

        executor.cancel_proposal_orders(proposal, open_orders)

        client.cancel_order.assert_called_once_with("order-123")

    def test_dry_run_does_not_call_cancel(self):
        open_orders = [{"symbol": "AAPL", "id": "order-789", "type": "limit"}]
        client = _mock_mcp(open_orders=open_orders)
        executor = OrderExecutor(mcp_client=client)
        executor._dry_run = True
        proposal = _make_proposal(symbol="AAPL")

        executor.cancel_proposal_orders(proposal, open_orders)

        client.cancel_order.assert_not_called()

    def test_no_orders_for_symbol_is_safe(self):
        client = _mock_mcp()
        executor = OrderExecutor(mcp_client=client)
        executor._dry_run = False
        proposal = _make_proposal(symbol="AAPL")

        executor.cancel_proposal_orders(proposal, [])

        client.cancel_order.assert_not_called()
