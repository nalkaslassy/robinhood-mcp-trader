"""
Defensive Monitor — intraday circuit-breaker checks.

Runs 2-3x during market hours as part of the monitoring cycle.
Checks for:
  - VIX intraday spike >= VIX_INTRADAY_SPIKE_PCT
  - Any held position swinging >= POSITION_INTRADAY_SWING_PCT intraday

If either condition fires, new entry proposals are suppressed for the rest of
that trading day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from trading_agent import config
from trading_agent.account_state import Position


@dataclass
class IntradayBar:
    """Minimal intraday snapshot for a single symbol."""
    symbol: str
    open_price: float
    current_price: float
    high: float
    low: float

    @property
    def intraday_swing_pct(self) -> float:
        """Absolute % move from open to current."""
        if self.open_price == 0:
            return 0.0
        return abs(self.current_price - self.open_price) / self.open_price


@dataclass
class DefensiveStatus:
    vix_spike: bool
    vix_open: float
    vix_current: float
    vix_spike_pct: float
    positions_with_swings: List[str]
    should_pause: bool
    reason: str


class DefensiveMonitor:
    def __init__(self, market_data_client=None):
        self._client = market_data_client

    # ------------------------------------------------------------------
    # VIX check
    # ------------------------------------------------------------------

    def check_vix_spike(self, vix_open: float, vix_current: float) -> bool:
        """
        Return True if the VIX has moved up >= VIX_INTRADAY_SPIKE_PCT from open.
        Only upward spikes (risk-on-off) are relevant.
        """
        if vix_open <= 0:
            return False
        change_pct = (vix_current - vix_open) / vix_open
        # round to 10 decimal places to avoid floating-point boundary misses
        return round(change_pct, 10) >= config.VIX_INTRADAY_SPIKE_PCT

    # ------------------------------------------------------------------
    # Position swing check
    # ------------------------------------------------------------------

    def check_position_swings(
        self, intraday_bars: List[IntradayBar]
    ) -> List[str]:
        """Return symbols whose intraday swing exceeds the threshold."""
        flagged = []
        for bar in intraday_bars:
            if bar.intraday_swing_pct >= config.POSITION_INTRADAY_SWING_PCT:
                flagged.append(bar.symbol)
        return flagged

    # ------------------------------------------------------------------
    # Combined gate
    # ------------------------------------------------------------------

    def should_pause_new_entries(
        self,
        vix_open: float,
        vix_current: float,
        intraday_bars: Optional[List[IntradayBar]] = None,
    ) -> Tuple[bool, str]:
        """
        Return (pause, reason).  True means: do NOT open any new positions today.
        """
        reasons = []

        vix_spike = self.check_vix_spike(vix_open, vix_current)
        if vix_spike:
            spike_pct = (vix_current - vix_open) / vix_open if vix_open > 0 else 0
            reasons.append(
                f"VIX intraday spike {spike_pct:.1%} (open={vix_open:.1f}, now={vix_current:.1f})"
            )

        swinging = []
        if intraday_bars:
            swinging = self.check_position_swings(intraday_bars)
            if swinging:
                reasons.append(
                    f"Positions with >{config.POSITION_INTRADAY_SWING_PCT:.0%} intraday swing: {', '.join(swinging)}"
                )

        vix_spike_pct = (vix_current - vix_open) / vix_open if vix_open > 0 else 0.0
        status_should_pause = bool(reasons)

        return status_should_pause, " | ".join(reasons) if reasons else "No defensive triggers active"

    def get_full_status(
        self,
        vix_open: float,
        vix_current: float,
        intraday_bars: Optional[List[IntradayBar]] = None,
    ) -> DefensiveStatus:
        vix_spike = self.check_vix_spike(vix_open, vix_current)
        vix_pct = (vix_current - vix_open) / vix_open if vix_open > 0 else 0.0
        swinging = self.check_position_swings(intraday_bars or [])
        should_pause, reason = self.should_pause_new_entries(vix_open, vix_current, intraday_bars)

        return DefensiveStatus(
            vix_spike=vix_spike,
            vix_open=vix_open,
            vix_current=vix_current,
            vix_spike_pct=vix_pct,
            positions_with_swings=swinging,
            should_pause=should_pause,
            reason=reason,
        )
