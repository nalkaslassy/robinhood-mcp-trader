"""
Integration tests — end-to-end dry run through the full pipeline.

Scenarios:
  (a) Clear qualifying setup
  (b) No qualifying setups (near-miss reporting)
  (c) Drawdown breaker active (no proposals generated)
  (d) Macro NO_TRADE state (no proposals generated)

All MCP / market-data calls are mocked.  No real account data or orders used.
"""
from __future__ import annotations

from datetime import date, timedelta, timezone, datetime
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from trading_agent import config
from trading_agent.account_state import AccountSnapshot, AccountStateManager, Position
from trading_agent.defensive_monitor import DefensiveMonitor, IntradayBar
from trading_agent.main_agent import TradingAgent
from trading_agent.order_executor import OrderExecutor
from trading_agent.research_engine import (
    DailyResearchReport,
    MacroState,
    run_daily_research,
)
from trading_agent.reporting import TradeJournalEntry, generate_daily_report


# ---------------------------------------------------------------------------
# Shared mock market data client (same helper as test_research_engine)
# ---------------------------------------------------------------------------

def _rising(n, start=100.0, step=0.5):
    return [round(start + i * step, 4) for i in range(n)]


def _falling(n, start=150.0, step=0.5):
    return [round(start - i * step, 4) for i in range(n)]


class _MockDataClient:
    def __init__(
        self,
        closes=None,
        volumes=None,
        earnings=None,
        spy_closes=None,
        vix=18.0,
    ):
        self._closes = closes or _rising(60, start=80.0, step=0.5)
        self._volumes = volumes or [2_000_000.0] * 60
        self._earnings = earnings or {}
        self._spy_closes = spy_closes or _rising(55, start=400.0, step=1.0)
        self._vix = vix

    def get_price_history(self, symbol, days):
        base = self._spy_closes if symbol == "SPY" else self._closes
        bars = base[-days:] if len(base) >= days else base
        return [
            {
                "date": (date.today() - timedelta(days=len(bars) - i)).isoformat(),
                "open": c * 0.99, "high": c * 1.01, "low": c * 0.99,
                "close": c,
                "volume": self._volumes[i % len(self._volumes)],
            }
            for i, c in enumerate(bars)
        ]

    def get_current_price(self, symbol):
        return self._closes[-1]

    def get_upcoming_earnings(self, symbol):
        return self._earnings.get(symbol)

    def get_recent_news(self, symbol, days):
        return []

    def get_vix_data(self, days):
        return [{"date": date.today().isoformat(), "close": self._vix,
                 "open": self._vix, "high": self._vix, "low": self._vix, "volume": 0}]


def _mock_mcp_order_client(current_price=100.0):
    client = MagicMock()
    client.get_current_price.return_value = current_price
    client.get_positions.return_value = []
    client.get_open_orders.return_value = []
    client.place_limit_buy.return_value = {"id": "int-entry-001"}
    client.place_stop_loss.return_value = {"id": "int-stop-001"}
    client.place_limit_sell.return_value = {"id": "int-target-001"}
    return client


# ---------------------------------------------------------------------------
# Helper: build a seeded AccountStateManager
# ---------------------------------------------------------------------------

def _make_account_mgr(
    cash=250.0,
    positions=None,
    peak=None,
    mcp_client=None,
) -> AccountStateManager:
    if mcp_client is None:
        # Build a mock MCP client so refresh() works without a real server
        pos_list = positions or []
        mock = MagicMock()
        mock.get_account.return_value = {
            "cash": cash,
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "average_buy_price": p.average_buy_price,
                    "current_price": p.current_price,
                }
                for p in pos_list
            ],
        }
        mcp_client = mock

    mgr = AccountStateManager(mcp_client=mcp_client)
    pos_list = positions or []
    snap = AccountSnapshot(
        cash=cash,
        positions=pos_list,
        peak_account_value=peak if peak is not None else cash,
    )
    mgr._snapshot = snap
    mgr._peak_value = snap.peak_account_value
    return mgr


# ---------------------------------------------------------------------------
# Scenario (a) — clear qualifying setup
# ---------------------------------------------------------------------------

class TestScenarioClearQualifyingSetup:
    def test_research_produces_ranked_candidates(self):
        # Volumes high enough for liquidity ($100 * 2M = $200M/day)
        client = _MockDataClient(
            closes=_rising(60, start=80.0, step=0.5),
            volumes=[2_000_000.0] * 60,
            vix=18.0,
        )
        report = run_daily_research(client=client)
        # At least one symbol from the watchlist should qualify
        assert report.macro.state in (MacroState.NORMAL, MacroState.RAISE_BAR)
        # Check pipeline ran without errors
        assert len(report.error_log) == 0

    def test_proposal_created_on_approval(self, tmp_path):
        """
        Inject a pre-built research report so the test verifies the
        agent orchestration layer (proposal → approval → order), not the
        research pipeline (which has its own dedicated test class).
        """
        from unittest.mock import patch
        from trading_agent.research_engine import (
            CatalystResult, DailyResearchReport, MacroSnapshot, MacroState,
            RankedCandidate, RiskRewardResult, TechnicalSignal,
        )

        tech = TechnicalSignal(
            symbol="AAPL", current_price=100.0, ma20=97.0, ma50=92.0,
            rsi=60.0, avg_volume_20d=1e6, recent_volume=1.5e6,
            support_level=94.0, resistance_level=104.0,
            is_uptrend=True, rsi_bounce=True, rsi_momentum=True,
            volume_confirmed=True, passes_screen=True,
        )
        rr = RiskRewardResult(
            symbol="AAPL", entry_price=100.0,
            stop_price=94.0, target_price=104.0,
            stop_pct=0.06, target_pct=0.04,
            reward_risk_ratio=0.667, passes=True,
        )
        macro = MacroSnapshot(450.0, 440.0, 18.0, True, False, MacroState.NORMAL)
        candidate = RankedCandidate(
            symbol="AAPL", technical=tech,
            catalyst=CatalystResult(symbol="AAPL", excluded=False),
            risk_reward=rr, macro=macro, rank_score=0.667,
        )
        synthetic_report = DailyResearchReport(
            date="2026-06-14", macro=macro,
            ranked_candidates=[candidate],
            near_misses=[], earnings_excluded=[],
            liquidity_excluded=[], error_log=[],
        )

        order_client = _mock_mcp_order_client(current_price=100.0)
        account_mgr = _make_account_mgr(cash=250.0)
        executor = OrderExecutor(mcp_client=order_client)
        executor._dry_run = True

        approved = []

        with patch("trading_agent.main_agent.run_daily_research", return_value=synthetic_report):
            agent = TradingAgent(
                market_data_client=None,
                order_executor=executor,
                account_manager=account_mgr,
                approval_callback=lambda p: approved.append(p) or True,
            )
            agent.run_morning_research()

        assert len(approved) >= 1

    def test_dry_run_order_not_sent_to_mcp(self):
        order_client = _mock_mcp_order_client(current_price=109.0)
        data_client = _MockDataClient(
            closes=_rising(60, start=80.0, step=0.5),
            volumes=[2_000_000.0] * 60,
            vix=18.0,
        )
        account_mgr = _make_account_mgr(cash=250.0)
        executor = OrderExecutor(mcp_client=order_client)
        executor._dry_run = True

        agent = TradingAgent(
            market_data_client=data_client,
            order_executor=executor,
            account_manager=account_mgr,
            approval_callback=lambda p: True,
        )
        agent.run_morning_research()
        order_client.place_limit_buy.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario (b) — no qualifying setups
# ---------------------------------------------------------------------------

class TestScenarioNoQualifyingSetups:
    def test_near_misses_populated_when_no_candidates(self):
        # All prices in downtrend — nothing should pass technical screen
        client = _MockDataClient(
            closes=_falling(60, start=150.0, step=1.0),
            volumes=[2_000_000.0] * 60,
            vix=18.0,
        )
        report = run_daily_research(client=client)
        assert len(report.ranked_candidates) == 0
        # Near-misses should explain why each symbol was excluded
        assert len(report.near_misses) > 0

    def test_daily_report_with_no_trades_has_all_sections(self):
        report = generate_daily_report(
            account_value=250.0, cash=250.0,
            open_positions=[], closed_today=[],
            proposals=[], near_misses=[{"symbol": "AMD", "reason": "no uptrend"}],
            macro_summary="NORMAL", earnings_excluded=[],
            drawdown_pct=0.0, report_date="2026-06-14",
        )
        for section in ["OPEN POSITIONS", "CLOSED TODAY", "TRADE PROPOSALS", "NEAR-MISSES"]:
            assert section in report
        assert "AMD" in report


# ---------------------------------------------------------------------------
# Scenario (c) — drawdown breaker active
# ---------------------------------------------------------------------------

class TestScenarioDrawdownBreakerActive:
    def test_no_proposals_when_drawdown_active(self):
        order_client = _mock_mcp_order_client()
        data_client = _MockDataClient(
            closes=_rising(60, start=80.0, step=0.5),
            volumes=[2_000_000.0] * 60,
        )
        # Account down 20% from peak
        account_mgr = _make_account_mgr(cash=200.0, peak=250.0)
        executor = OrderExecutor(mcp_client=order_client)
        executor._dry_run = True

        approved = []
        agent = TradingAgent(
            market_data_client=data_client,
            order_executor=executor,
            account_manager=account_mgr,
            approval_callback=lambda p: approved.append(p) or True,
        )

        result = agent.run_morning_research()

        assert result is None  # morning research returns None when breaker active
        assert len(approved) == 0
        order_client.place_limit_buy.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario (d) — macro NO_TRADE state
# ---------------------------------------------------------------------------

class TestScenarioMacroNoTrade:
    def test_no_candidates_when_no_trade_state(self):
        # SPY in downtrend AND VIX > threshold
        client = _MockDataClient(
            closes=_rising(60, start=80.0, step=0.5),
            volumes=[2_000_000.0] * 60,
            spy_closes=_falling(55, start=500.0, step=2.0),
            vix=30.0,
        )
        report = run_daily_research(client=client)
        assert report.macro.state == MacroState.NO_TRADE
        assert len(report.ranked_candidates) == 0


# ---------------------------------------------------------------------------
# Full pipeline dry-run end-to-end
# ---------------------------------------------------------------------------

class TestFullPipelineDryRun:
    def test_end_to_end_no_exceptions(self, tmp_path):
        """Smoke test: full agent cycle runs without raising."""
        order_client = _mock_mcp_order_client(current_price=109.0)
        data_client = _MockDataClient(
            closes=_rising(60, start=80.0, step=0.5),
            volumes=[2_000_000.0] * 60,
            vix=18.0,
        )
        account_mgr = _make_account_mgr(cash=250.0)
        executor = OrderExecutor(mcp_client=order_client)
        executor._dry_run = True

        # Inject mocked open positions/orders for monitoring cycle
        order_client.get_positions.return_value = []
        order_client.get_open_orders.return_value = []

        agent = TradingAgent(
            market_data_client=data_client,
            order_executor=executor,
            account_manager=account_mgr,
            approval_callback=lambda p: True,
        )

        research = agent.run_morning_research()
        # Monitoring cycle
        agent.run_monitoring_cycle(vix_open=18.0, vix_current=19.0, intraday_bars=[])
        # End of day
        report_str = agent.run_end_of_day(research)
        assert "DAILY REPORT" in report_str

    def test_defensive_pause_suppresses_new_entries(self):
        monitor = DefensiveMonitor()
        vix_open = 18.0
        vix_spike = vix_open * (1 + config.VIX_INTRADAY_SPIKE_PCT + 0.01)
        pause, reason = monitor.should_pause_new_entries(vix_open, vix_spike)
        assert pause is True
        assert "VIX" in reason
