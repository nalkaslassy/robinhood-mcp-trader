"""
Order Executor — Robinhood MCP integration layer.

ALL live order placement lives here.  Tests inject a MockMCPOrderClient that
records calls without touching any real account.

SAFETY INVARIANT: place_bracket_order() must ONLY be called after a
TradeProposal has received explicit human approval.  This module enforces:
  - The proposal must not be expired
  - The current price must still be within the proposal's entry range
  - DRY_RUN mode logs the intended order without sending it
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from trading_agent import config
from trading_agent.account_state import Position
from trading_agent.trade_proposal import TradeProposal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    stop_order_id: Optional[str] = None
    target_order_id: Optional[str] = None
    message: str = ""
    dry_run: bool = False


@dataclass
class BracketIntegrityResult:
    symbol: str
    position_ok: bool
    stop_order_active: bool
    target_order_active: bool
    emergency: bool = False
    emergency_reason: str = ""


# ---------------------------------------------------------------------------
# MCP client protocol (thin interface — swap for real MCP at integration time)
# ---------------------------------------------------------------------------

class MCPOrderClient:
    """Interface definition only — real implementation connects to Robinhood MCP."""

    def get_account(self) -> dict: ...

    def get_positions(self) -> List[dict]: ...

    def get_open_orders(self) -> List[dict]: ...

    def place_limit_buy(
        self,
        symbol: str,
        quantity: float,
        limit_price: float,
    ) -> dict: ...

    def place_stop_loss(
        self,
        symbol: str,
        quantity: float,
        stop_price: float,
    ) -> dict: ...

    def place_limit_sell(
        self,
        symbol: str,
        quantity: float,
        limit_price: float,
    ) -> dict: ...

    def cancel_order(self, order_id: str) -> dict: ...

    def get_current_price(self, symbol: str) -> float: ...


# ---------------------------------------------------------------------------
# Order executor
# ---------------------------------------------------------------------------

class OrderExecutor:
    def __init__(self, mcp_client: Optional[MCPOrderClient] = None):
        self._client = mcp_client
        self._dry_run = config.DRY_RUN

    # ------------------------------------------------------------------
    # Position queries
    # ------------------------------------------------------------------

    def check_open_positions(self) -> List[Position]:
        raw = self._client.get_positions()
        return [
            Position(
                symbol=p["symbol"],
                quantity=float(p["quantity"]),
                average_buy_price=float(p["average_buy_price"]),
                current_price=float(p["current_price"]),
                is_leveraged_etf=p["symbol"] in config.LEVERAGED_ETFS,
            )
            for p in raw
        ]

    def check_open_orders(self) -> List[dict]:
        return self._client.get_open_orders()

    # ------------------------------------------------------------------
    # Bracket order placement
    # ------------------------------------------------------------------

    def place_bracket_order(
        self,
        proposal: TradeProposal,
        *,
        approval_timestamp: Optional[datetime] = None,
    ) -> OrderResult:
        """
        Place entry limit order + OCO stop/target for an APPROVED proposal.

        Pre-checks:
          1. Proposal must not be expired at the time of this call.
          2. Current price must be within the proposal's entry range.
          3. DRY_RUN mode: log only, return success without sending.

        Robinhood does not natively support OCO orders.  We place the entry
        buy limit, then immediately place both a stop-loss limit-sell and a
        take-profit limit-sell.  The monitoring cycle (check_bracket_integrity)
        detects when one fires and cancels the other.
        """
        now = approval_timestamp or datetime.now(tz=timezone.utc)

        if proposal.is_expired(now=now):
            return OrderResult(
                success=False,
                message=f"Proposal for {proposal.symbol} has expired — cannot place order.",
            )

        current_price = self._client.get_current_price(proposal.symbol)
        if not proposal.is_price_in_range(current_price):
            return OrderResult(
                success=False,
                message=(
                    f"Current price ${current_price:.2f} outside entry range "
                    f"${proposal.entry_price_low:.2f}–${proposal.entry_price_high:.2f}. "
                    "Proposal invalidated — regenerate with fresh prices."
                ),
            )

        from trading_agent.position_sizing import shares_from_dollar_amount
        qty = shares_from_dollar_amount(proposal.position_size_dollars, current_price)

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would place bracket order: "
                "%s qty=%.4f entry=%.2f stop=%.2f target=%.2f",
                proposal.symbol, qty,
                proposal.entry_midpoint, proposal.stop_price, proposal.target_price,
            )
            return OrderResult(
                success=True,
                order_id="DRY_RUN_ENTRY",
                stop_order_id="DRY_RUN_STOP",
                target_order_id="DRY_RUN_TARGET",
                message=f"[DRY RUN] Bracket order for {proposal.symbol} logged (not sent).",
                dry_run=True,
            )

        # Live path
        try:
            entry_result = self._client.place_limit_buy(
                proposal.symbol, qty, proposal.entry_midpoint
            )
            entry_id = entry_result.get("id", "unknown")

            stop_result = self._client.place_stop_loss(
                proposal.symbol, qty, proposal.stop_price
            )
            stop_id = stop_result.get("id", "unknown")

            target_result = self._client.place_limit_sell(
                proposal.symbol, qty, proposal.target_price
            )
            target_id = target_result.get("id", "unknown")

            return OrderResult(
                success=True,
                order_id=entry_id,
                stop_order_id=stop_id,
                target_order_id=target_id,
                message=f"Bracket order placed for {proposal.symbol}: entry={entry_id}",
            )
        except Exception as e:
            return OrderResult(
                success=False,
                message=f"Order placement failed for {proposal.symbol}: {e}",
            )

    # ------------------------------------------------------------------
    # Bracket integrity check
    # ------------------------------------------------------------------

    def check_bracket_integrity(
        self,
        position: Position,
        open_orders: List[dict],
    ) -> BracketIntegrityResult:
        """
        Verify stop and target orders exist for a held position.

        Emergency flag fires when position is at or beyond its theoretical stop
        (≥ STOP_LOSS_PCT_MAX drawdown) AND no active stop order is found.
        """
        symbol_orders = [o for o in open_orders if o.get("symbol") == position.symbol]

        stop_active = any(
            o.get("type") in ("stop", "stop_loss", "stop_limit")
            for o in symbol_orders
        )
        target_active = any(
            o.get("type") in ("limit", "limit_sell")
            and float(o.get("price", 0)) > position.current_price
            for o in symbol_orders
        )

        # Emergency: position has dropped >= max stop % AND no stop order found
        pnl_pct = position.unrealized_pnl_pct
        emergency = (pnl_pct <= -config.STOP_LOSS_PCT_MAX) and not stop_active
        emergency_reason = ""
        if emergency:
            emergency_reason = (
                f"{position.symbol} is down {pnl_pct:.1%} with NO active stop order — "
                "EMERGENCY: manual intervention required!"
            )
            logger.critical(emergency_reason)

        return BracketIntegrityResult(
            symbol=position.symbol,
            position_ok=not emergency,
            stop_order_active=stop_active,
            target_order_active=target_active,
            emergency=emergency,
            emergency_reason=emergency_reason,
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cancel_proposal_orders(self, proposal: TradeProposal, open_orders: List[dict]) -> None:
        """
        Cancel any stray orders for the proposal's symbol when a proposal
        expires without approval.  Safe to call even if no orders exist.
        """
        symbol_orders = [o for o in open_orders if o.get("symbol") == proposal.symbol]
        for order in symbol_orders:
            order_id = order.get("id")
            if order_id:
                try:
                    if not self._dry_run:
                        self._client.cancel_order(order_id)
                    logger.info("Cancelled stray order %s for %s", order_id, proposal.symbol)
                except Exception as e:
                    logger.error("Failed to cancel order %s: %s", order_id, e)
