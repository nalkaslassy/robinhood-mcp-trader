"""Tests for reporting.py — all I/O uses tmp_path."""
from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from trading_agent import config
from trading_agent.reporting import (
    TradeJournalEntry,
    generate_daily_report,
    generate_monthly_report,
    generate_weekly_report,
    load_journal_entries,
    log_trade_journal_entry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(
    symbol="AAPL",
    pnl=10.0,
    outcome="target",
    date_str=None,
    entry_price=100.0,
    exit_price=None,
) -> TradeJournalEntry:
    if date_str is None:
        date_str = date.today().isoformat()
    if exit_price is None:
        exit_price = entry_price + pnl
    qty = 1.0
    pnl_pct = pnl / entry_price
    return TradeJournalEntry(
        date=date_str,
        symbol=symbol,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=qty,
        pnl=pnl,
        pnl_pct=pnl_pct,
        stop_price=entry_price * 0.94,
        target_price=entry_price * 1.03,
        outcome=outcome,
        setup_note="test setup",
    )


def _dummy_proposals():
    return [
        {
            "proposal": {
                "symbol": "NVDA",
                "entry_low": 180.0,
                "entry_high": 182.0,
                "stop": 168.0,
                "target": 187.0,
            },
            "status": "approved",
        }
    ]


# ---------------------------------------------------------------------------
# Journal persistence
# ---------------------------------------------------------------------------

class TestJournalPersistence:
    def test_entry_written_and_reloaded(self, tmp_path):
        path = str(tmp_path / "journal.jsonl")
        e = _entry(symbol="TSLA", pnl=15.0)
        log_trade_journal_entry(e, path=path)
        loaded = load_journal_entries(path=path)
        assert len(loaded) == 1
        assert loaded[0].symbol == "TSLA"
        assert loaded[0].pnl == 15.0

    def test_multiple_entries_appended(self, tmp_path):
        path = str(tmp_path / "journal.jsonl")
        for sym in ["AAPL", "MSFT", "NVDA"]:
            log_trade_journal_entry(_entry(symbol=sym), path=path)
        loaded = load_journal_entries(path=path)
        assert len(loaded) == 3
        assert {e.symbol for e in loaded} == {"AAPL", "MSFT", "NVDA"}

    def test_empty_file_returns_empty_list(self, tmp_path):
        path = str(tmp_path / "empty.jsonl")
        assert load_journal_entries(path=path) == []


# ---------------------------------------------------------------------------
# Daily report
# ---------------------------------------------------------------------------

class TestDailyReport:
    def _base_report(self, **kwargs):
        defaults = dict(
            account_value=250.0,
            cash=175.0,
            open_positions=[],
            closed_today=[],
            proposals=[],
            near_misses=[],
            macro_summary="SPY above MA50 | VIX=18.0 | NORMAL",
            earnings_excluded=[],
            drawdown_pct=0.0,
            report_date="2026-06-14",
        )
        defaults.update(kwargs)
        return generate_daily_report(**defaults)

    def test_contains_date(self):
        report = self._base_report()
        assert "2026-06-14" in report

    def test_contains_account_value(self):
        report = self._base_report(account_value=312.50)
        assert "312.50" in report

    def test_no_trades_sections_present(self):
        report = self._base_report()
        assert "OPEN POSITIONS" in report
        assert "CLOSED TODAY" in report
        assert "TRADE PROPOSALS" in report
        assert "NEAR-MISSES" in report

    def test_near_miss_shown(self):
        report = self._base_report(near_misses=[{"symbol": "AMD", "reason": "no uptrend"}])
        assert "AMD" in report
        assert "no uptrend" in report

    def test_open_position_shown(self):
        pos = {
            "symbol": "NVDA",
            "quantity": 0.5,
            "avg_price": 180.0,
            "current_price": 185.0,
            "unrealized_pnl_pct": 0.028,
        }
        report = self._base_report(open_positions=[pos])
        assert "NVDA" in report
        assert "185.00" in report

    def test_closed_trade_shown(self):
        t = _entry(symbol="TSLA", pnl=12.0, outcome="target")
        report = self._base_report(closed_today=[t])
        assert "TSLA" in report
        assert "target" in report

    def test_proposal_shown_with_status(self):
        report = self._base_report(proposals=_dummy_proposals())
        assert "NVDA" in report
        assert "APPROVED" in report

    def test_earnings_excluded_shown(self):
        report = self._base_report(earnings_excluded=["SMCI", "PLTR"])
        assert "SMCI" in report
        assert "PLTR" in report

    def test_no_trades_no_proposals_still_complete(self):
        report = self._base_report()
        # All section headers must be present even when empty
        for section in ["OPEN POSITIONS", "CLOSED TODAY", "TRADE PROPOSALS", "NEAR-MISSES"]:
            assert section in report


# ---------------------------------------------------------------------------
# Weekly report
# ---------------------------------------------------------------------------

def _make_week_entries():
    today = date.today()
    return [
        _entry("AAPL", pnl=8.0, outcome="target", date_str=(today - timedelta(days=3)).isoformat()),
        _entry("MSFT", pnl=-5.0, outcome="stop", date_str=(today - timedelta(days=2)).isoformat()),
        _entry("NVDA", pnl=12.0, outcome="target", date_str=(today - timedelta(days=1)).isoformat()),
    ]


class TestWeeklyReport:
    def test_aggregates_trade_count(self):
        entries = _make_week_entries()
        today = date.today()
        start = (today - timedelta(days=6)).isoformat()
        end = today.isoformat()
        report = generate_weekly_report(entries, start, end, 250.0, 265.0, 265.0)
        assert "3" in report  # 3 trades

    def test_win_rate_computed(self):
        entries = _make_week_entries()  # 2 wins, 1 loss => 67%
        today = date.today()
        start = (today - timedelta(days=6)).isoformat()
        end = today.isoformat()
        report = generate_weekly_report(entries, start, end, 250.0, 265.0, 265.0)
        assert "67%" in report

    def test_realized_pnl_shown(self):
        entries = _make_week_entries()  # 8 - 5 + 12 = 15
        today = date.today()
        start = (today - timedelta(days=6)).isoformat()
        end = today.isoformat()
        report = generate_weekly_report(entries, start, end, 250.0, 265.0, 265.0)
        assert "+15.00" in report or "15.00" in report

    def test_vs_starting_capital_shown(self):
        today = date.today()
        start = (today - timedelta(days=6)).isoformat()
        end = today.isoformat()
        report = generate_weekly_report([], start, end, 250.0, 275.0, 275.0)
        # 275/250 = +10% vs starting capital
        assert "+10.0%" in report or "10.0%" in report


# ---------------------------------------------------------------------------
# Monthly report
# ---------------------------------------------------------------------------

def _make_month_entries(month="2026-06"):
    return [
        _entry("AAPL", pnl=10.0, outcome="target", date_str=f"{month}-01"),
        _entry("MSFT", pnl=-6.0, outcome="stop", date_str=f"{month}-05"),
        _entry("NVDA", pnl=14.0, outcome="target", date_str=f"{month}-10"),
        _entry("TSLA", pnl=-4.0, outcome="stop", date_str=f"{month}-15"),
        _entry("AMD", pnl=9.0, outcome="target", date_str=f"{month}-20"),
    ]


class TestMonthlyReport:
    def test_win_rate(self):
        entries = _make_month_entries()  # 3 wins, 2 losses => 60%
        report = generate_monthly_report(entries, "2026-06", 250.0, 273.0, 0.03, 275.0)
        assert "60%" in report

    def test_avg_win_loss(self):
        entries = _make_month_entries()
        # avg win = (10+14+9)/3 = 11
        # avg loss = (-6-4)/2 = -5
        report = generate_monthly_report(entries, "2026-06", 250.0, 273.0, 0.03, 275.0)
        assert "+11.00" in report
        assert "-5.00" in report

    def test_spy_benchmark_shown(self):
        entries = _make_month_entries()
        report = generate_monthly_report(entries, "2026-06", 250.0, 273.0, 0.05, 275.0)
        assert "SPY" in report
        assert "5.0%" in report or "+5.0%" in report

    def test_alpha_computed(self):
        entries = _make_month_entries()
        # account return = (273-250)/250 = 9.2%; SPY = 3%
        report = generate_monthly_report(entries, "2026-06", 250.0, 273.0, 0.03, 275.0)
        assert "Alpha" in report

    def test_no_trades_report_still_complete(self):
        report = generate_monthly_report([], "2026-06", 250.0, 250.0, 0.02, 250.0)
        assert "MONTHLY REPORT" in report
        assert "no closed trades" in report

    def test_recommendation_section_present(self):
        entries = _make_month_entries()
        report = generate_monthly_report(entries, "2026-06", 250.0, 273.0, 0.03, 275.0)
        assert "Recommendation" in report
