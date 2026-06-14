"""
Account State Manager — fetches and tracks account health.

Live data comes from the Robinhood MCP client injected at construction.
Tests inject a MockMCPClient that returns synthetic data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from trading_agent import config


@dataclass
class Position:
    symbol: str
    quantity: float
    average_buy_price: float
    current_price: float
    is_leveraged_etf: bool = False

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.average_buy_price == 0:
            return 0.0
        return (self.current_price - self.average_buy_price) / self.average_buy_price


@dataclass
class AccountSnapshot:
    cash: float
    positions: List[Position] = field(default_factory=list)
    peak_account_value: float = 0.0

    @property
    def positions_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    @property
    def total_value(self) -> float:
        return self.cash + self.positions_value

    @property
    def drawdown_pct(self) -> float:
        if self.peak_account_value <= 0:
            return 0.0
        return (self.peak_account_value - self.total_value) / self.peak_account_value

    @property
    def leveraged_etf_count(self) -> int:
        return sum(1 for p in self.positions if p.is_leveraged_etf)


class AccountStateManager:
    def __init__(self, mcp_client=None):
        self._client = mcp_client
        self._peak_value: float = config.STARTING_CAPITAL
        self._snapshot: Optional[AccountSnapshot] = None

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def refresh(self) -> AccountSnapshot:
        """Fetch current state from MCP and update snapshot."""
        raw = self._client.get_account()
        cash = float(raw["cash"])
        raw_positions = raw.get("positions", [])

        positions = [
            Position(
                symbol=p["symbol"],
                quantity=float(p["quantity"]),
                average_buy_price=float(p["average_buy_price"]),
                current_price=float(p["current_price"]),
                is_leveraged_etf=p["symbol"] in config.LEVERAGED_ETFS,
            )
            for p in raw_positions
        ]

        snapshot = AccountSnapshot(
            cash=cash,
            positions=positions,
            peak_account_value=self._peak_value,
        )

        # Update rolling peak
        if snapshot.total_value > self._peak_value:
            self._peak_value = snapshot.total_value
            snapshot.peak_account_value = self._peak_value

        self._snapshot = snapshot
        return snapshot

    def get_snapshot(self) -> AccountSnapshot:
        if self._snapshot is None:
            raise RuntimeError("Call refresh() before accessing snapshot.")
        return self._snapshot

    # ------------------------------------------------------------------
    # Decision helpers
    # ------------------------------------------------------------------

    def is_drawdown_breaker_active(self) -> bool:
        snap = self.get_snapshot()
        return snap.drawdown_pct >= config.ACCOUNT_DRAWDOWN_BREAKER_PCT

    def can_open_new_position(
        self,
        symbol: str = "",
        is_leveraged_etf: bool = False,
    ) -> Tuple[bool, str]:
        snap = self.get_snapshot()

        if self.is_drawdown_breaker_active():
            return False, (
                f"Drawdown breaker active: {snap.drawdown_pct:.1%} drawdown "
                f"from peak ${snap.peak_account_value:.2f}"
            )

        if len(snap.positions) >= config.MAX_CONCURRENT_POSITIONS:
            return False, (
                f"Max concurrent positions reached ({config.MAX_CONCURRENT_POSITIONS})"
            )

        if is_leveraged_etf and snap.leveraged_etf_count >= config.MAX_LEVERAGED_ETF_POSITIONS:
            return False, (
                f"Max leveraged ETF positions reached ({config.MAX_LEVERAGED_ETF_POSITIONS})"
            )

        min_position = snap.total_value * config.POSITION_SIZE_PCT_MIN
        required_cash = snap.total_value * config.CASH_RESERVE_PCT_MIN + min_position
        if snap.cash < required_cash:
            return False, (
                f"Insufficient cash: ${snap.cash:.2f} available, "
                f"${required_cash:.2f} required (reserve + min position)"
            )

        return True, "OK"

    def position_size_range(self) -> Tuple[float, float]:
        snap = self.get_snapshot()
        low = snap.total_value * config.POSITION_SIZE_PCT_MIN
        high = snap.total_value * config.POSITION_SIZE_PCT_MAX
        return low, high
