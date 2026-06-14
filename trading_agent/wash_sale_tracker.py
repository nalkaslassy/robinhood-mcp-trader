"""
Wash-sale tracker — maintains a JSON-lines log of closed trades and flags
potential wash-sale violations before re-entry.

A wash sale occurs when the *same* (or substantially identical) security is
repurchased within 30 days of a sale at a *loss*.  The tracker only disallows
re-entry based on loss records; gain-close records are irrelevant.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import List, Optional, Tuple

from trading_agent import config


@dataclass
class ClosedTrade:
    symbol: str
    close_date: str   # ISO format YYYY-MM-DD
    pnl: float        # positive = gain, negative = loss
    entry_price: float
    exit_price: float
    quantity: float


class WashSaleTracker:
    def __init__(self, log_path: Optional[str] = None):
        self._path = log_path or config.TRADE_JOURNAL_PATH
        self._records: List[ClosedTrade] = []
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self):
        if not os.path.exists(self._path):
            return
        self._records = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("record_type") == "closed_trade":
                    self._records.append(ClosedTrade(**{
                        k: v for k, v in data.items() if k != "record_type"
                    }))

    def record_closed_trade(self, trade: ClosedTrade):
        """Append a closed trade to the persistent log and in-memory list."""
        self._records.append(trade)
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            row = {"record_type": "closed_trade", **asdict(trade)}
            f.write(json.dumps(row) + "\n")

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def check_wash_sale(
        self,
        symbol: str,
        today: Optional[date] = None,
    ) -> Tuple[bool, int]:
        """
        Return (would_trigger, days_remaining).

        would_trigger is True when re-entering `symbol` today would be within
        WASH_SALE_WINDOW_DAYS of the most recent loss-close on that symbol.
        days_remaining is how many more days must pass before it clears.
        """
        if today is None:
            today = date.today()

        window = timedelta(days=config.WASH_SALE_WINDOW_DAYS)
        latest_loss_date: Optional[date] = None

        for record in self._records:
            if record.symbol != symbol:
                continue
            if record.pnl >= 0:
                continue  # gain — irrelevant to wash-sale rules
            closed = date.fromisoformat(record.close_date)
            if latest_loss_date is None or closed > latest_loss_date:
                latest_loss_date = closed

        if latest_loss_date is None:
            return False, 0

        clear_date = latest_loss_date + window
        if today < clear_date:
            days_remaining = (clear_date - today).days
            return True, days_remaining

        return False, 0

    def get_loss_records(self, symbol: str) -> List[ClosedTrade]:
        return [r for r in self._records if r.symbol == symbol and r.pnl < 0]
