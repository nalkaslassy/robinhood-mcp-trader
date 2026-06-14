"""
Position sizing and bracket-price calculations.
All math is pure — no I/O, easy to unit-test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from trading_agent import config


@dataclass
class BracketPrices:
    stop_price: float
    target_price: float
    stop_pct: float
    target_pct: float


def calculate_position_size(account_value: float) -> Tuple[float, float]:
    """Return (min_dollars, max_dollars) for a new position."""
    if account_value <= 0:
        raise ValueError(f"account_value must be positive, got {account_value}")
    low = round(account_value * config.POSITION_SIZE_PCT_MIN, 2)
    high = round(account_value * config.POSITION_SIZE_PCT_MAX, 2)
    return low, high


def calculate_bracket_prices(
    entry_price: float,
    stop_pct: float,
    target_pct: float,
) -> BracketPrices:
    """
    Compute stop and target prices from entry and percentage offsets.

    Raises ValueError if stop_pct or target_pct fall outside their configured
    bounds, ensuring we never place a bracket that violates risk rules.
    """
    if not (config.STOP_LOSS_PCT_MIN <= stop_pct <= config.STOP_LOSS_PCT_MAX):
        raise ValueError(
            f"stop_pct {stop_pct:.3f} outside [{config.STOP_LOSS_PCT_MIN}, {config.STOP_LOSS_PCT_MAX}]"
        )
    if not (config.PROFIT_TARGET_PCT_MIN <= target_pct <= config.PROFIT_TARGET_PCT_MAX):
        raise ValueError(
            f"target_pct {target_pct:.3f} outside [{config.PROFIT_TARGET_PCT_MIN}, {config.PROFIT_TARGET_PCT_MAX}]"
        )
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")

    stop_price = round(entry_price * (1 - stop_pct), 4)
    target_price = round(entry_price * (1 + target_pct), 4)
    return BracketPrices(
        stop_price=stop_price,
        target_price=target_price,
        stop_pct=stop_pct,
        target_pct=target_pct,
    )


def shares_from_dollar_amount(dollar_amount: float, price: float) -> float:
    """Fractional shares allowed on Robinhood — return exact float qty."""
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    if dollar_amount <= 0:
        raise ValueError(f"dollar_amount must be positive, got {dollar_amount}")
    return round(dollar_amount / price, 6)


def clamp_stop_pct(raw_stop_pct: float) -> float:
    """Clamp a desired stop % into the valid range, rounding to 4 decimals."""
    clamped = max(config.STOP_LOSS_PCT_MIN, min(config.STOP_LOSS_PCT_MAX, raw_stop_pct))
    return round(clamped, 4)


def clamp_target_pct(raw_target_pct: float) -> float:
    """Clamp a desired target % into the valid range, rounding to 4 decimals."""
    clamped = max(config.PROFIT_TARGET_PCT_MIN, min(config.PROFIT_TARGET_PCT_MAX, raw_target_pct))
    return round(clamped, 4)
