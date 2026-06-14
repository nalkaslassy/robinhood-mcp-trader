"""Tests for wash_sale_tracker.py — all file I/O is bypassed with tmp_path."""
import json
import os
from datetime import date, timedelta

import pytest

from trading_agent.wash_sale_tracker import ClosedTrade, WashSaleTracker
from trading_agent import config


def _make_tracker(tmp_path) -> WashSaleTracker:
    log = str(tmp_path / "journal.jsonl")
    return WashSaleTracker(log_path=log)


def _loss_trade(symbol: str, close_date: date, pnl: float = -10.0) -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol,
        close_date=close_date.isoformat(),
        pnl=pnl,
        entry_price=100.0,
        exit_price=100.0 + pnl,
        quantity=1.0,
    )


def _gain_trade(symbol: str, close_date: date) -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol,
        close_date=close_date.isoformat(),
        pnl=15.0,
        entry_price=100.0,
        exit_price=115.0,
        quantity=1.0,
    )


class TestWashSaleTracker:
    def test_no_history_returns_false(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        triggered, days = tracker.check_wash_sale("AAPL")
        assert triggered is False
        assert days == 0

    def test_re_entry_within_window_triggers(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        close = date.today() - timedelta(days=10)
        tracker.record_closed_trade(_loss_trade("AAPL", close))
        triggered, days = tracker.check_wash_sale("AAPL", today=date.today())
        assert triggered is True
        assert days == config.WASH_SALE_WINDOW_DAYS - 10

    def test_re_entry_exactly_at_window_boundary_is_clear(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        close = date.today() - timedelta(days=config.WASH_SALE_WINDOW_DAYS)
        tracker.record_closed_trade(_loss_trade("AAPL", close))
        triggered, days = tracker.check_wash_sale("AAPL", today=date.today())
        # close_date + 30 days == today => clear_date == today, not strictly future
        assert triggered is False
        assert days == 0

    def test_re_entry_after_window_is_clear(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        close = date.today() - timedelta(days=config.WASH_SALE_WINDOW_DAYS + 1)
        tracker.record_closed_trade(_loss_trade("AAPL", close))
        triggered, days = tracker.check_wash_sale("AAPL", today=date.today())
        assert triggered is False
        assert days == 0

    def test_gain_close_does_not_trigger_wash_sale(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        close = date.today() - timedelta(days=5)
        tracker.record_closed_trade(_gain_trade("AAPL", close))
        triggered, days = tracker.check_wash_sale("AAPL", today=date.today())
        assert triggered is False

    def test_different_symbol_not_affected(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        close = date.today() - timedelta(days=5)
        tracker.record_closed_trade(_loss_trade("AAPL", close))
        triggered, days = tracker.check_wash_sale("MSFT", today=date.today())
        assert triggered is False

    def test_uses_most_recent_loss_date(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        older = date.today() - timedelta(days=25)
        newer = date.today() - timedelta(days=5)
        tracker.record_closed_trade(_loss_trade("NVDA", older))
        tracker.record_closed_trade(_loss_trade("NVDA", newer))
        triggered, days = tracker.check_wash_sale("NVDA", today=date.today())
        assert triggered is True
        assert days == config.WASH_SALE_WINDOW_DAYS - 5

    def test_records_persisted_and_reloaded(self, tmp_path):
        log = str(tmp_path / "journal.jsonl")
        t1 = WashSaleTracker(log_path=log)
        close = date.today() - timedelta(days=10)
        t1.record_closed_trade(_loss_trade("TSLA", close))

        t2 = WashSaleTracker(log_path=log)
        triggered, days = t2.check_wash_sale("TSLA", today=date.today())
        assert triggered is True

    def test_gain_record_in_file_not_loaded_as_loss(self, tmp_path):
        log = str(tmp_path / "journal.jsonl")
        tracker = WashSaleTracker(log_path=log)
        close = date.today() - timedelta(days=3)
        tracker.record_closed_trade(_gain_trade("AMD", close))

        tracker2 = WashSaleTracker(log_path=log)
        triggered, _ = tracker2.check_wash_sale("AMD", today=date.today())
        assert triggered is False
