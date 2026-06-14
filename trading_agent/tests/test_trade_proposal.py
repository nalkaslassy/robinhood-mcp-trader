"""Tests for trade_proposal.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_agent import config
from trading_agent.research_engine import (
    CatalystResult,
    MacroSnapshot,
    MacroState,
    RankedCandidate,
    RiskRewardResult,
    TechnicalSignal,
)
from trading_agent.trade_proposal import TradeProposal, create_proposal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(symbol="AAPL", price=100.0, stop_pct=0.06, target_pct=0.03):
    tech = TechnicalSignal(
        symbol=symbol,
        current_price=price,
        ma20=price * 0.97,
        ma50=price * 0.92,
        rsi=60.0,
        avg_volume_20d=1_000_000.0,
        recent_volume=1_500_000.0,
        support_level=price * (1 - stop_pct),
        resistance_level=price * (1 + target_pct),
        is_uptrend=True,
        rsi_bounce=True,
        rsi_momentum=True,
        volume_confirmed=True,
        passes_screen=True,
    )
    rr = RiskRewardResult(
        symbol=symbol,
        entry_price=price,
        stop_price=round(price * (1 - stop_pct), 4),
        target_price=round(price * (1 + target_pct), 4),
        stop_pct=stop_pct,
        target_pct=target_pct,
        reward_risk_ratio=round(target_pct / stop_pct, 4),
        passes=True,
    )
    macro = MacroSnapshot(
        spy_price=450.0,
        spy_ma50=440.0,
        vix_level=18.0,
        spy_uptrend=True,
        vix_high=False,
        state=MacroState.NORMAL,
    )
    return RankedCandidate(
        symbol=symbol,
        technical=tech,
        catalyst=CatalystResult(symbol=symbol, excluded=False),
        risk_reward=rr,
        macro=macro,
        rank_score=rr.reward_risk_ratio,
    )


# ---------------------------------------------------------------------------
# Expiration tests
# ---------------------------------------------------------------------------

class TestProposalExpiration:
    def test_not_expired_when_fresh(self):
        now = datetime.now(tz=timezone.utc)
        candidate = _make_candidate()
        proposal = create_proposal(candidate, 62.50, created_at=now)
        # Check just after creation
        assert proposal.is_expired(now=now + timedelta(minutes=1)) is False

    def test_expired_after_expiry_window(self):
        now = datetime.now(tz=timezone.utc)
        candidate = _make_candidate()
        proposal = create_proposal(candidate, 62.50, created_at=now)
        check_time = now + timedelta(hours=config.ENTRY_RECOMMENDATION_EXPIRY_HOURS + 1)
        assert proposal.is_expired(now=check_time) is True

    def test_expired_exactly_at_expiry_time(self):
        now = datetime.now(tz=timezone.utc)
        candidate = _make_candidate()
        proposal = create_proposal(candidate, 62.50, created_at=now)
        exactly_at_expiry = now + timedelta(hours=config.ENTRY_RECOMMENDATION_EXPIRY_HOURS)
        assert proposal.is_expired(now=exactly_at_expiry) is True

    def test_not_expired_one_second_before(self):
        now = datetime.now(tz=timezone.utc)
        candidate = _make_candidate()
        proposal = create_proposal(candidate, 62.50, created_at=now)
        just_before = now + timedelta(hours=config.ENTRY_RECOMMENDATION_EXPIRY_HOURS) - timedelta(seconds=1)
        assert proposal.is_expired(now=just_before) is False


# ---------------------------------------------------------------------------
# Price range tests
# ---------------------------------------------------------------------------

class TestProposalPriceRange:
    def test_price_in_range(self):
        candidate = _make_candidate(price=100.0)
        proposal = create_proposal(candidate, 62.50)
        # Entry spread is ±0.5%, so range ~99.5 to 100.5
        assert proposal.is_price_in_range(100.0) is True
        assert proposal.is_price_in_range(99.6) is True

    def test_price_above_range(self):
        candidate = _make_candidate(price=100.0)
        proposal = create_proposal(candidate, 62.50)
        assert proposal.is_price_in_range(102.0) is False

    def test_price_below_range(self):
        candidate = _make_candidate(price=100.0)
        proposal = create_proposal(candidate, 62.50)
        assert proposal.is_price_in_range(98.0) is False


# ---------------------------------------------------------------------------
# Proposal content tests
# ---------------------------------------------------------------------------

class TestProposalContent:
    def test_proposal_fields_populated(self):
        candidate = _make_candidate(symbol="NVDA", price=200.0, stop_pct=0.06, target_pct=0.03)
        proposal = create_proposal(candidate, 100.0)
        assert proposal.symbol == "NVDA"
        assert proposal.stop_price == pytest.approx(200.0 * 0.94, rel=1e-4)
        assert proposal.target_price == pytest.approx(200.0 * 1.03, rel=1e-4)
        assert proposal.position_size_dollars == 100.0
        assert proposal.reward_risk_ratio > 0
        assert "NVDA" in proposal.reasoning

    def test_wash_sale_flag_included(self):
        candidate = _make_candidate()
        candidate = RankedCandidate(
            symbol=candidate.symbol,
            technical=candidate.technical,
            catalyst=candidate.catalyst,
            risk_reward=candidate.risk_reward,
            macro=candidate.macro,
            rank_score=candidate.rank_score,
            wash_sale_flag=True,
            wash_sale_days_remaining=15,
        )
        proposal = create_proposal(candidate, 62.50)
        assert proposal.wash_sale_flag is True
        assert proposal.wash_sale_days_remaining == 15
        assert "WASH SALE" in proposal.reasoning

    def test_leveraged_etf_flag(self):
        candidate = _make_candidate(symbol="SOXL")
        proposal = create_proposal(candidate, 62.50)
        assert proposal.is_leveraged_etf is True

    def test_regular_symbol_not_flagged_leveraged(self):
        candidate = _make_candidate(symbol="AAPL")
        proposal = create_proposal(candidate, 62.50)
        assert proposal.is_leveraged_etf is False
