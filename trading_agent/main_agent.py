"""
Main Agent — orchestrates the full daily trading workflow.

Workflow (daily):
  1. Morning: run research, generate proposal(s)
  2. Present proposal(s) to human for approval
  3. On approval: place bracket order
  4. On expiry without approval: discard, log "not_taken"
  5. Throughout market hours: defensive monitor + bracket integrity checks
  6. End of day: overnight hold check, generate daily report
  7. Weekly/monthly: trigger corresponding reports on schedule

This module is the entry point for live operation.  In DRY_RUN mode (default)
no real orders are placed.  Human approval is simulated via stdin prompts;
replace with a webhook / notification service for production use.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

from trading_agent import config
from trading_agent.account_state import AccountStateManager, Position
from trading_agent.defensive_monitor import DefensiveMonitor, IntradayBar
from trading_agent.order_executor import OrderExecutor
from trading_agent.position_sizing import calculate_position_size
from trading_agent.reporting import (
    TradeJournalEntry,
    generate_daily_report,
    log_trade_journal_entry,
)
from trading_agent.research_engine import (
    DailyResearchReport,
    MacroState,
    RankedCandidate,
    run_daily_research,
)
from trading_agent.trade_proposal import (
    TradeProposal,
    create_proposal,
    format_proposal_for_display,
)
from trading_agent.wash_sale_tracker import WashSaleTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class TradingAgent:
    def __init__(
        self,
        market_data_client=None,
        order_executor: Optional[OrderExecutor] = None,
        account_manager: Optional[AccountStateManager] = None,
        wash_sale_tracker: Optional[WashSaleTracker] = None,
        approval_callback=None,
    ):
        self._data_client = market_data_client
        self._executor = order_executor or OrderExecutor(mcp_client=None)
        self._account = account_manager or AccountStateManager(mcp_client=None)
        self._wash = wash_sale_tracker or WashSaleTracker()
        self._monitor = DefensiveMonitor()
        # approval_callback(proposal) -> bool; defaults to stdin prompt
        self._approval_callback = approval_callback or self._stdin_approval

        self._active_proposals: List[TradeProposal] = []
        self._closed_today: List[TradeJournalEntry] = []

    # ------------------------------------------------------------------
    # Morning research cycle
    # ------------------------------------------------------------------

    def run_morning_research(self) -> Optional[DailyResearchReport]:
        logger.info("=== Morning research cycle starting ===")

        try:
            self._account.refresh()
        except Exception as e:
            logger.error("Failed to refresh account state: %s", e)
            return None

        snap = self._account.get_snapshot()
        logger.info(
            "Account: $%.2f total | $%.2f cash | %d position(s)",
            snap.total_value, snap.cash, len(snap.positions),
        )

        if self._account.is_drawdown_breaker_active():
            logger.warning(
                "DRAWDOWN BREAKER ACTIVE (%.1f%% from peak) — skipping all new entries.",
                snap.drawdown_pct * 100,
            )
            return None

        report = run_daily_research(
            client=self._data_client,
            wash_sale_checker=lambda sym: self._wash.check_wash_sale(sym),
            date_str=date.today().isoformat(),
        )

        logger.info("Macro state: %s", report.macro.state.value)
        logger.info(
            "Candidates: %d ranked, %d near-misses, %d earnings-excluded",
            len(report.ranked_candidates),
            len(report.near_misses),
            len(report.earnings_excluded),
        )

        for candidate in report.ranked_candidates:
            self._process_candidate(candidate, snap.total_value)

        return report

    def _process_candidate(self, candidate: RankedCandidate, account_value: float):
        is_lev = candidate.symbol in config.LEVERAGED_ETFS
        ok, reason = self._account.can_open_new_position(
            symbol=candidate.symbol, is_leveraged_etf=is_lev
        )
        if not ok:
            logger.info("Skipping %s — %s", candidate.symbol, reason)
            return

        low, high = calculate_position_size(account_value)
        proposal = create_proposal(candidate, position_size_dollars=(low + high) / 2)

        logger.info("\n%s", format_proposal_for_display(proposal))

        if self._approval_callback(proposal):
            self._execute_approved_proposal(proposal)
        else:
            logger.info("Proposal for %s not approved / expired.", proposal.symbol)

    # ------------------------------------------------------------------
    # Order execution (only called after human approval)
    # ------------------------------------------------------------------

    def _execute_approved_proposal(self, proposal: TradeProposal):
        result = self._executor.place_bracket_order(proposal)
        if result.success:
            logger.info(
                "Bracket order placed for %s: entry=%s stop=%s target=%s%s",
                proposal.symbol,
                result.order_id, result.stop_order_id, result.target_order_id,
                " [DRY RUN]" if result.dry_run else "",
            )
        else:
            logger.error("Order FAILED for %s: %s", proposal.symbol, result.message)

    # ------------------------------------------------------------------
    # Intraday monitoring cycle (call 2-3x during market hours)
    # ------------------------------------------------------------------

    def run_monitoring_cycle(
        self,
        vix_open: float,
        vix_current: float,
        intraday_bars: Optional[List[IntradayBar]] = None,
    ):
        logger.info("=== Monitoring cycle ===")

        pause, reason = self._monitor.should_pause_new_entries(
            vix_open, vix_current, intraday_bars
        )
        if pause:
            logger.warning("DEFENSIVE PAUSE: %s", reason)

        try:
            positions = self._executor.check_open_positions()
            open_orders = self._executor.check_open_orders()
        except Exception as e:
            logger.error("Failed to fetch positions/orders: %s", e)
            return

        for position in positions:
            result = self._executor.check_bracket_integrity(position, open_orders)
            if result.emergency:
                logger.critical("BRACKET EMERGENCY: %s", result.emergency_reason)
            elif not result.stop_order_active:
                logger.warning("%s: stop order not found — verify manually!", position.symbol)

    # ------------------------------------------------------------------
    # End-of-day
    # ------------------------------------------------------------------

    def run_end_of_day(self, research_report: Optional[DailyResearchReport] = None) -> str:
        logger.info("=== End-of-day cycle ===")

        try:
            snap = self._account.refresh()
        except Exception as e:
            logger.error("Failed to refresh account for EOD report: %s", e)
            snap = self._account.get_snapshot()

        open_pos_dicts = [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_price": p.average_buy_price,
                "current_price": p.current_price,
                "unrealized_pnl_pct": p.unrealized_pnl_pct,
            }
            for p in snap.positions
        ]

        proposals_summary = [
            {
                "proposal": {
                    "symbol": p.symbol,
                    "entry_low": p.entry_price_low,
                    "entry_high": p.entry_price_high,
                    "stop": p.stop_price,
                    "target": p.target_price,
                },
                "status": "expired" if p.is_expired() else "active",
            }
            for p in self._active_proposals
        ]

        macro_str = ""
        near_misses = []
        earnings_excluded = []
        if research_report:
            macro_str = (
                f"SPY {'above' if research_report.macro.spy_uptrend else 'below'} MA50 | "
                f"VIX={research_report.macro.vix_level:.1f} | {research_report.macro.state.value}"
            )
            near_misses = research_report.near_misses
            earnings_excluded = research_report.earnings_excluded

        report = generate_daily_report(
            account_value=snap.total_value,
            cash=snap.cash,
            open_positions=open_pos_dicts,
            closed_today=self._closed_today,
            proposals=proposals_summary,
            near_misses=near_misses,
            macro_summary=macro_str,
            earnings_excluded=earnings_excluded,
            drawdown_pct=snap.drawdown_pct,
            report_date=date.today().isoformat(),
        )
        print(report)
        return report

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stdin_approval(proposal: TradeProposal) -> bool:
        """Default human-approval gate: prompt via stdin."""
        print(format_proposal_for_display(proposal))
        try:
            answer = input("Approve this trade? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer == "y"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    logger.info("Trading agent starting (DRY_RUN=%s)", config.DRY_RUN)
    agent = TradingAgent()
    report = agent.run_morning_research()
    if report:
        agent.run_end_of_day(report)


if __name__ == "__main__":
    main()
