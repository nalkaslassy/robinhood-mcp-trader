"""
Main Agent — orchestrates the full daily trading workflow.

Workflow (daily):
  1. Morning: run research using WatchlistManager's active symbols
  2. Record daily outcomes back into WatchlistManager
  3. Friday: run weekly watchlist review via Claude Sonnet
  4. Present proposal(s) to human via SMS (Twilio) for approval
  5. On approval: place bracket order via Robinhood MCP
  6. Throughout market hours: defensive monitor + bracket integrity checks
  7. End of day: overnight hold check, generate daily report

Live vs dry-run: set DRY_RUN=False in config.py only after validating with
DRY_RUN=True for at least one full week.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import List, Optional

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
from trading_agent.watchlist_manager import WatchlistManager
from trading_agent.sms_notifier import SMSNotifier

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
        watchlist_manager: Optional[WatchlistManager] = None,
        sms_notifier: Optional[SMSNotifier] = None,
        approval_callback=None,
    ):
        self._data_client = market_data_client
        self._executor = order_executor or OrderExecutor(mcp_client=None)
        self._account = account_manager or AccountStateManager(mcp_client=None)
        self._wash = wash_sale_tracker or WashSaleTracker()
        self._monitor = DefensiveMonitor()
        self._watchlist = watchlist_manager or WatchlistManager()
        self._notifier = sms_notifier or SMSNotifier()

        # approval_callback(proposal) -> bool
        # Priority: explicit callback > SMSNotifier > stdin fallback
        if approval_callback is not None:
            self._approval_callback = approval_callback
        elif sms_notifier is not None:
            self._approval_callback = lambda p: sms_notifier.wait_for_approval(p)
        else:
            self._approval_callback = self._stdin_approval

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
            logger.warning(
                "Account refresh failed (%s) — continuing research with cached state.", e
            )

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
            watchlist_manager=self._watchlist,
        )

        logger.info("Macro state: %s", report.macro.state.value)
        logger.info(
            "Candidates: %d ranked, %d near-misses, %d earnings-excluded",
            len(report.ranked_candidates),
            len(report.near_misses),
            len(report.earnings_excluded),
        )

        # Record today's outcomes so the watchlist manager can track performance
        try:
            self._watchlist.record_daily_outcomes(report)
        except Exception as e:
            logger.error("watchlist record_daily_outcomes error: %s", e)

        # Weekly watchlist review (Fridays or first-ever run)
        if self._watchlist.should_run_weekly_review():
            self._run_weekly_watchlist_review()

        if report.ranked_candidates:
            for candidate in report.ranked_candidates:
                self._process_candidate(candidate, snap.total_value)
        else:
            # No setups today — send a brief daily summary so you know it ran
            top_reasons = {}
            for nm in report.near_misses:
                reason = nm["reason"].split(":")[1].strip() if ":" in nm["reason"] else nm["reason"]
                first_clause = reason.split(";")[0].strip()
                top_reasons[first_clause] = top_reasons.get(first_clause, 0) + 1
            reason_summary = ", ".join(
                f"{count}x {r}" for r, count in
                sorted(top_reasons.items(), key=lambda x: -x[1])[:3]
            )
            self._notifier.send_alert(
                f"Daily scan complete — no setups today.\n"
                f"Screened {len(report.near_misses)} stocks | "
                f"Macro: {report.macro.state.value} | VIX={report.macro.vix_level:.1f}\n"
                f"Common reasons: {reason_summary}"
            )

        return report

    def _run_weekly_watchlist_review(self):
        logger.info("=== Weekly watchlist review ===")
        popular: List[str] = []
        if self._data_client is not None and hasattr(self._data_client, "get_popular_watchlist_symbols"):
            try:
                popular = self._data_client.get_popular_watchlist_symbols()
                self._watchlist.register_candidates(popular)
            except Exception as e:
                logger.warning("Could not fetch popular watchlists: %s", e)
        try:
            decisions = self._watchlist.run_weekly_review(popular_candidates=popular)
            if decisions:
                reasoning = decisions.get("reasoning", "")
                logger.info("Watchlist review complete. Reasoning: %s", reasoning)
                active_count = len(self._watchlist.get_active_symbols())
                logger.info("Active watchlist now has %d symbols.", active_count)
                # Alert via SMS so you can see what changed
                kept    = len(decisions.get("keep", []))
                removed = len(decisions.get("remove", []))
                added   = len(decisions.get("add", []))
                self._notifier.send_alert(
                    f"Weekly watchlist review complete.\n"
                    f"Kept {kept} | Removed {removed} | Added {added}\n"
                    f"{reasoning}"
                )
        except Exception as e:
            logger.error("Weekly watchlist review error: %s", e)

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

        # Send proposal via SMS and wait for human approval
        self._notifier.send_proposal(proposal)
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
            self._notifier.send_confirmation(
                proposal.symbol, result.order_id, dry_run=result.dry_run
            )
        else:
            logger.error("Order FAILED for %s: %s", proposal.symbol, result.message)
            self._notifier.send_alert(
                f"Order FAILED for {proposal.symbol}: {result.message}"
            )

    # ------------------------------------------------------------------
    # Mid-day bracket monitor — run at 12:30 PM and 3:30 PM
    # ------------------------------------------------------------------

    def run_midday_monitor(self):
        """
        Cancels orphaned exit orders and alerts on missing stops.

        Robinhood has no native OCO support, so after a stop or target fills,
        the other order remains open with no shares behind it.  This job runs
        twice a day to detect and cancel those orphaned orders before they
        cause an unintended short sale.
        """
        logger.info("=== Mid-day bracket monitor ===")

        try:
            positions    = self._executor.check_open_positions()
            open_orders  = self._executor.check_open_orders()
        except Exception as e:
            logger.error("Could not fetch positions/orders: %s", e)
            self._notifier.send_alert(f"Monitor error — could not reach Robinhood: {e}")
            return

        position_symbols = {p.symbol for p in positions}
        cancelled: List[str] = []
        emergencies: List[str] = []

        # Cancel any sell order for a symbol where we hold no shares
        for order in open_orders:
            symbol     = order.get("symbol", "")
            side       = order.get("side", "")
            order_id   = order.get("id", "")
            order_type = order.get("type", "")

            if side != "sell" or symbol in position_symbols:
                continue

            logger.warning(
                "Orphaned %s sell order for %s (no position held) — cancelling %s",
                order_type, symbol, order_id,
            )
            try:
                if not config.DRY_RUN:
                    self._executor._client.cancel_order(order_id)
                cancelled.append(f"{symbol} {order_type}")
                logger.info("Cancelled orphaned order %s", order_id)
            except Exception as e:
                msg = f"FAILED to cancel orphaned {symbol} order {order_id}: {e}"
                logger.error(msg)
                emergencies.append(msg)

        # Verify every held position still has a stop order
        for position in positions:
            result = self._executor.check_bracket_integrity(position, open_orders)
            if result.emergency:
                emergencies.append(result.emergency_reason)
            elif not result.stop_order_active:
                emergencies.append(
                    f"{position.symbol} has NO active stop order — intervene manually!"
                )

        # Send WhatsApp summary
        if cancelled:
            self._notifier.send_alert(
                f"Mid-day cleanup: cancelled {len(cancelled)} orphaned order(s): "
                + ", ".join(cancelled)
            )
        for msg in emergencies:
            self._notifier.send_alert(f"BRACKET EMERGENCY: {msg}")

        if not cancelled and not emergencies:
            if positions:
                logger.info(
                    "Bracket OK — %d position(s), all stops confirmed.", len(positions)
                )
            else:
                logger.info("No open positions — nothing to monitor.")

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
            self._notifier.send_alert(f"DEFENSIVE PAUSE: {reason}")

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
                self._notifier.send_alert(
                    f"BRACKET EMERGENCY for {position.symbol}: {result.emergency_reason}"
                )
            elif not result.stop_order_active:
                logger.warning("%s: stop order not found — verify manually!", position.symbol)
                self._notifier.send_alert(
                    f"WARNING: {position.symbol} stop order not found. Verify manually."
                )

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
        """Fallback human-approval gate when Twilio is not configured."""
        print(format_proposal_for_display(proposal))
        try:
            answer = input("Approve this trade? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer == "y"


# ---------------------------------------------------------------------------
# CLI entry point — builds live clients from environment variables
# ---------------------------------------------------------------------------

def _build_live_agent() -> TradingAgent:
    """
    Wire all live components together.
    Requires: ANTHROPIC_API_KEY
    For order placement: ROBINHOOD_MCP_TOKEN (only used when a trade fires)
    Optional: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, YOUR_PHONE_NUMBER
    """
    import anthropic
    from trading_agent.yfinance_client import YFinanceDataClient
    from trading_agent.robinhood_mcp_client import RobinhoodOrderClient

    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Free data via yfinance — no API cost for daily research
    market_data  = YFinanceDataClient()
    order_client = RobinhoodOrderClient(claude=claude)
    notifier     = SMSNotifier()
    watchlist    = WatchlistManager(anthropic_client=claude)

    executor     = OrderExecutor(mcp_client=order_client)
    account_mgr  = AccountStateManager(mcp_client=order_client)

    return TradingAgent(
        market_data_client=market_data,
        order_executor=executor,
        account_manager=account_mgr,
        wash_sale_tracker=WashSaleTracker(),
        watchlist_manager=watchlist,
        sms_notifier=notifier,
    )


def main():
    # Load .env from the project root so plain `python -m trading_agent.main_agent` works
    try:
        from pathlib import Path as _Path
        from dotenv import load_dotenv
        _env = _Path(__file__).parent.parent / ".env"
        load_dotenv(_env, override=True)
    except Exception:
        pass

    mode = sys.argv[1] if len(sys.argv) > 1 else "research"

    if mode == "monitor":
        logger.info("Mid-day monitor starting (DRY_RUN=%s)", config.DRY_RUN)
        agent = _build_live_agent()
        agent.run_midday_monitor()
    else:
        logger.info("Trading agent starting (DRY_RUN=%s)", config.DRY_RUN)
        agent = _build_live_agent()
        report = agent.run_morning_research()
        if report:
            agent.run_end_of_day(report)


if __name__ == "__main__":
    main()
