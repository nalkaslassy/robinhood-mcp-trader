"""
Daily Research Engine — 6-step pipeline.

All market data is fetched via an injected `MarketDataClient` whose interface
is defined here.  Tests inject a `MockMarketDataClient`; live usage will
inject a Robinhood-MCP-backed client.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from trading_agent import config


# ---------------------------------------------------------------------------
# Market data protocol (interface for MCP or mock injection)
# ---------------------------------------------------------------------------

@runtime_checkable
class MarketDataClient(Protocol):
    def get_price_history(self, symbol: str, days: int) -> List[Dict]:
        """Return list of {date, open, high, low, close, volume} dicts, oldest first."""
        ...

    def get_current_price(self, symbol: str) -> float: ...

    def get_upcoming_earnings(self, symbol: str) -> Optional[str]:
        """Return ISO date string of next earnings, or None."""
        ...

    def get_recent_news(self, symbol: str, days: int) -> List[Dict]:
        """Return list of {date, headline} dicts."""
        ...

    def get_vix_data(self, days: int) -> List[Dict]:
        """Return price-history-style dicts for VIX."""
        ...

    def get_intraday_volume(self, symbol: str) -> Optional[float]:
        """Return today's cumulative intraday volume, or None if unavailable."""
        ...


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------

class MacroState(Enum):
    NORMAL = "NORMAL"
    RAISE_BAR = "RAISE_BAR"
    NO_TRADE = "NO_TRADE"


@dataclass
class TechnicalSignal:
    symbol: str
    current_price: float
    ma20: float
    ma50: float
    rsi: float
    avg_volume_20d: float
    recent_volume: float
    support_level: Optional[float]
    resistance_level: Optional[float]
    atr: float               # 14-day Average True Range
    adx: Optional[float]     # 14-day ADX (None if insufficient data)
    # Gate flags
    is_uptrend: bool         # price > MA50
    is_trending: bool        # ADX > 25
    rsi_bounce: bool         # RSI recently crossed up from below 30
    rsi_momentum: bool       # RSI > 50 with upward slope
    volume_confirmed: bool   # recent volume > avg_volume_20d
    passes_screen: bool      # overall pass/fail for Step 2
    exclusion_reason: str = ""


@dataclass
class CatalystResult:
    symbol: str
    excluded: bool
    exclusion_reason: str = ""
    earnings_date: Optional[str] = None
    avg_dollar_volume: float = 0.0
    recent_news: List[Dict] = field(default_factory=list)


@dataclass
class RiskRewardResult:
    symbol: str
    entry_price: float
    stop_price: float
    target_price: float
    stop_pct: float
    target_pct: float
    reward_risk_ratio: float
    passes: bool
    exclusion_reason: str = ""


@dataclass
class MacroSnapshot:
    spy_price: float
    spy_ma50: float
    vix_level: float
    spy_uptrend: bool
    vix_high: bool
    state: MacroState


@dataclass
class RankedCandidate:
    symbol: str
    technical: TechnicalSignal
    catalyst: CatalystResult
    risk_reward: RiskRewardResult
    macro: MacroSnapshot
    rank_score: float         # higher is better; currently == reward_risk_ratio
    wash_sale_flag: bool = False
    wash_sale_days_remaining: int = 0


@dataclass
class DailyResearchReport:
    date: str
    macro: MacroSnapshot
    ranked_candidates: List[RankedCandidate]
    near_misses: List[Dict]       # {symbol, reason}
    earnings_excluded: List[str]
    liquidity_excluded: List[str]
    error_log: List[str]


# ---------------------------------------------------------------------------
# Indicator math helpers
# ---------------------------------------------------------------------------

def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder RSI.  Returns None if fewer than period+1 data points."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _rsi_series(closes: List[float], period: int = 14) -> List[float]:
    """Return RSI value for each bar where it can be computed."""
    results = []
    if len(closes) < period + 1:
        return results
    for end in range(period + 1, len(closes) + 1):
        val = _rsi(closes[:end], period)
        if val is not None:
            results.append(val)
    return results


def _atr_series(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    """Average True Range series using Wilder smoothing."""
    if len(closes) < 2:
        return []
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return []
    results = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        results.append((results[-1] * (period - 1) + tr) / period)
    return results


def _adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """
    Wilder ADX. Returns the current ADX value or None if insufficient data.
    ADX > 25 = trending, ADX < 20 = ranging/choppy.
    """
    if len(closes) < period * 2 + 1:
        return None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(closes)):
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up   if up > down and up > 0   else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    def _wilder_smooth(values: List[float], p: int) -> List[float]:
        if len(values) < p:
            return []
        smoothed = [sum(values[:p])]
        for v in values[p:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / p + v)
        return smoothed

    s_tr   = _wilder_smooth(trs, period)
    s_plus = _wilder_smooth(plus_dm, period)
    s_minus = _wilder_smooth(minus_dm, period)

    if not s_tr:
        return None

    dx_vals = []
    for i in range(len(s_tr)):
        if s_tr[i] == 0:
            continue
        plus_di  = 100 * s_plus[i]  / s_tr[i]
        minus_di = 100 * s_minus[i] / s_tr[i]
        denom = plus_di + minus_di
        if denom == 0:
            continue
        dx_vals.append(100 * abs(plus_di - minus_di) / denom)

    if len(dx_vals) < period:
        return None

    adx = sum(dx_vals[:period]) / period
    for dx in dx_vals[period:]:
        adx = (adx * (period - 1) + dx) / period
    return round(adx, 2)


def _detect_rsi_bounce(rsi_series: List[float], lookback: int = 5) -> bool:
    """
    True if within the last `lookback` bars, RSI crossed UP through 30
    (was below 30, then rose above 30).
    """
    if len(rsi_series) < lookback + 1:
        return False
    window = rsi_series[-(lookback + 1):]
    for i in range(len(window) - 1):
        if window[i] < 30 and window[i + 1] >= 30:
            return True
    return False


def _detect_rsi_momentum(rsi_series: List[float], lookback: int = 3) -> bool:
    """True if RSI is above 50 and rising over the last `lookback` bars."""
    if len(rsi_series) < lookback:
        return False
    recent = rsi_series[-lookback:]
    return recent[-1] > 50 and all(recent[i] <= recent[i + 1] for i in range(len(recent) - 1))


def _find_support(highs: List[float], lows: List[float], current: float, lookback: int = 20) -> Optional[float]:
    """
    Simple pivot-low support: lowest local low in the lookback window that is
    below current price.  Returns None if no suitable level found.
    """
    relevant = [l for l in lows[-lookback:] if l < current]
    if not relevant:
        return None
    return max(relevant)  # nearest (highest) low below current price


def _find_resistance(highs: List[float], current: float, lookback: int = 20) -> Optional[float]:
    """
    Simple pivot-high resistance: lowest high in the lookback window that is
    above current price.  Returns None if no suitable level found.
    """
    relevant = [h for h in highs[-lookback:] if h > current]
    if not relevant:
        return None
    return min(relevant)


def _market_minutes_elapsed() -> float:
    """Minutes since NYSE open (9:30 AM ET) today. Returns 0 before market open, caps at 390."""
    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et < market_open:
        return 0.0
    return min((now_et - market_open).total_seconds() / 60.0, 390.0)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def scan_universe(watchlist_manager=None) -> List[str]:
    """Step 1 — return the active watchlist from WatchlistManager, or the seed list."""
    if watchlist_manager is not None:
        return watchlist_manager.get_active_symbols()
    return list(config.WATCHLIST_SEED)


def technical_screen(symbol: str, client: MarketDataClient) -> TechnicalSignal:
    """Step 2 — compute technical indicators and assess pass/fail."""
    # 90 days keeps MA50 stable and gives 50-day S/R enough history
    history = client.get_price_history(symbol, days=90)
    closes  = [float(bar["close"])  for bar in history]
    volumes = [float(bar["volume"]) for bar in history]
    highs   = [float(bar["high"])   for bar in history]
    lows    = [float(bar["low"])    for bar in history]

    current_price = closes[-1] if closes else client.get_current_price(symbol)

    ma20 = _sma(closes, config.MA_SHORT_PERIOD) or 0.0
    ma50 = _sma(closes, config.MA_LONG_PERIOD)  or 0.0

    rsi_vals    = _rsi_series(closes, config.RSI_PERIOD)
    current_rsi = rsi_vals[-1] if rsi_vals else 50.0

    avg_vol    = _sma(volumes, config.MA_SHORT_PERIOD) or 0.0
    recent_vol = float(volumes[-1]) if volumes else 0.0

    # Intraday volume pace: project today's partial volume to a full-day equivalent.
    # This lets the 10:30/11:30 scans confirm volume BEFORE yesterday's bar is the only signal.
    intraday_vol = client.get_intraday_volume(symbol) if hasattr(client, "get_intraday_volume") else None
    minutes_elapsed = _market_minutes_elapsed()
    if intraday_vol is not None and minutes_elapsed >= 15:
        projected_vol = intraday_vol * (390.0 / minutes_elapsed)
    else:
        projected_vol = 0.0

    # Compute ATR and ADX for informational fields (not used as gates)
    atr_vals    = _atr_series(highs, lows, closes, config.RSI_PERIOD)
    current_atr = atr_vals[-1] if atr_vals else 0.0
    current_adx = _adx(highs, lows, closes, config.RSI_PERIOD)
    is_trending = (current_adx is not None) and (current_adx >= config.ADX_TREND_THRESHOLD)

    # S/R uses 50-day lookback (up from 20) for more reliable structure
    support    = _find_support(highs, lows, current_price, lookback=config.SR_LOOKBACK)
    resistance = _find_resistance(highs, current_price,    lookback=config.SR_LOOKBACK)

    is_uptrend       = (ma50 > 0) and (current_price > ma50)
    rsi_bounce       = _detect_rsi_bounce(rsi_vals)
    rsi_momentum     = _detect_rsi_momentum(rsi_vals)
    # Volume confirmed if yesterday beat average OR today is on pace to beat average
    volume_confirmed = avg_vol > 0 and (recent_vol >= avg_vol or projected_vol >= avg_vol)

    # Stop is valid when a support level exists within the configured % range
    has_valid_stop = (
        support is not None
        and config.STOP_LOSS_PCT_MIN <= (current_price - support) / current_price <= config.STOP_LOSS_PCT_MAX
    )

    # Both RSI momentum AND volume must confirm (not either/or)
    passes = is_uptrend and rsi_momentum and volume_confirmed and has_valid_stop
    reason = ""
    if not passes:
        parts = []
        if not is_uptrend:
            parts.append("no uptrend (price below MA50)")
        if not rsi_momentum:
            parts.append("RSI not momentum (need >50 and rising)")
        if not volume_confirmed:
            parts.append("volume below 20-day average")
        if not has_valid_stop:
            parts.append(f"no support within {config.STOP_LOSS_PCT_MIN:.0%}–{config.STOP_LOSS_PCT_MAX:.0%} stop range")
        reason = "; ".join(parts)

    return TechnicalSignal(
        symbol=symbol,
        current_price=current_price,
        ma20=ma20,
        ma50=ma50,
        rsi=current_rsi,
        avg_volume_20d=avg_vol,
        recent_volume=recent_vol,
        support_level=support,
        resistance_level=resistance,
        atr=current_atr,
        adx=current_adx,
        is_uptrend=is_uptrend,
        is_trending=is_trending,
        rsi_bounce=rsi_bounce,
        rsi_momentum=rsi_momentum,
        volume_confirmed=volume_confirmed,
        passes_screen=passes,
        exclusion_reason=reason,
    )


def catalyst_check(symbol: str, client: MarketDataClient) -> CatalystResult:
    """Step 3 — earnings-proximity and liquidity hard-excludes."""
    earnings_date = client.get_upcoming_earnings(symbol)
    news = client.get_recent_news(symbol, days=3)
    history = client.get_price_history(symbol, days=config.MA_SHORT_PERIOD + 5)

    # Avg dollar volume over 20 days
    dollar_vols = [
        float(bar["close"]) * float(bar["volume"])
        for bar in history[-config.MA_SHORT_PERIOD:]
    ]
    avg_dollar_vol = sum(dollar_vols) / len(dollar_vols) if dollar_vols else 0.0

    from datetime import date, timedelta
    today = date.today()

    if earnings_date:
        earnings_dt = date.fromisoformat(earnings_date)
        days_away = (earnings_dt - today).days
        if 0 <= days_away <= config.EARNINGS_EXCLUSION_WINDOW_DAYS:
            return CatalystResult(
                symbol=symbol,
                excluded=True,
                exclusion_reason=f"Earnings in {days_away} days ({earnings_date})",
                earnings_date=earnings_date,
                avg_dollar_volume=avg_dollar_vol,
                recent_news=news,
            )

    if avg_dollar_vol < config.MIN_AVG_DAILY_DOLLAR_VOLUME:
        return CatalystResult(
            symbol=symbol,
            excluded=True,
            exclusion_reason=(
                f"Avg daily dollar volume ${avg_dollar_vol:,.0f} "
                f"below ${config.MIN_AVG_DAILY_DOLLAR_VOLUME:,.0f} floor"
            ),
            earnings_date=earnings_date,
            avg_dollar_volume=avg_dollar_vol,
            recent_news=news,
        )

    return CatalystResult(
        symbol=symbol,
        excluded=False,
        earnings_date=earnings_date,
        avg_dollar_volume=avg_dollar_vol,
        recent_news=news,
    )


def risk_reward_calc(
    symbol: str,
    technical: TechnicalSignal,
) -> RiskRewardResult:
    """Step 4 — compute stop/target and validate risk:reward."""
    entry = technical.current_price

    if technical.support_level is None:
        return RiskRewardResult(
            symbol=symbol,
            entry_price=entry,
            stop_price=0.0,
            target_price=0.0,
            stop_pct=0.0,
            target_pct=0.0,
            reward_risk_ratio=0.0,
            passes=False,
            exclusion_reason="No support level identified",
        )

    stop_pct = (entry - technical.support_level) / entry

    if not (config.STOP_LOSS_PCT_MIN <= stop_pct <= config.STOP_LOSS_PCT_MAX):
        return RiskRewardResult(
            symbol=symbol,
            entry_price=entry,
            stop_price=round(entry * (1 - stop_pct), 4),
            target_price=0.0,
            stop_pct=stop_pct,
            target_pct=0.0,
            reward_risk_ratio=0.0,
            passes=False,
            exclusion_reason=(
                f"Stop distance {stop_pct:.1%} outside "
                f"[{config.STOP_LOSS_PCT_MIN:.0%}, {config.STOP_LOSS_PCT_MAX:.0%}]"
            ),
        )

    # Target is the actual resistance level — no artificial floors.
    # Reject if resistance is too close, too far, or not found at all.
    if technical.resistance_level is None:
        return RiskRewardResult(
            symbol=symbol,
            entry_price=entry,
            stop_price=round(entry * (1 - stop_pct), 4),
            target_price=0.0,
            stop_pct=stop_pct,
            target_pct=0.0,
            reward_risk_ratio=0.0,
            passes=False,
            exclusion_reason="No resistance level found — cannot set a target",
        )

    raw_target_pct = (technical.resistance_level - entry) / entry

    if raw_target_pct < config.PROFIT_TARGET_PCT_MIN:
        return RiskRewardResult(
            symbol=symbol,
            entry_price=entry,
            stop_price=round(entry * (1 - stop_pct), 4),
            target_price=0.0,
            stop_pct=stop_pct,
            target_pct=raw_target_pct,
            reward_risk_ratio=0.0,
            passes=False,
            exclusion_reason=f"Resistance too close ({raw_target_pct:.1%}) — not worth the trade",
        )

    # Cap at maximum but never invent upside that isn't there
    target_pct = min(config.PROFIT_TARGET_PCT_MAX, raw_target_pct)

    stop_price = round(entry * (1 - stop_pct), 4)
    target_price = round(entry * (1 + target_pct), 4)
    rr_ratio = round(target_pct / stop_pct, 4) if stop_pct > 0 else 0.0

    passes = rr_ratio >= 1.5  # minimum 1.5:1 reward:risk for positive expectancy

    return RiskRewardResult(
        symbol=symbol,
        entry_price=entry,
        stop_price=stop_price,
        target_price=target_price,
        stop_pct=stop_pct,
        target_pct=target_pct,
        reward_risk_ratio=rr_ratio,
        passes=passes,
        exclusion_reason="" if passes else f"Poor reward:risk ({rr_ratio:.2f})",
    )


def macro_sentiment_check(client: MarketDataClient) -> MacroSnapshot:
    """Step 5 — SPY trend + VIX level -> 3-tier macro gate."""
    spy_history = client.get_price_history("SPY", days=55)
    spy_closes = [float(b["close"]) for b in spy_history]
    spy_price = spy_closes[-1] if spy_closes else 0.0
    spy_ma50 = _sma(spy_closes, config.MA_LONG_PERIOD) or 0.0

    vix_history = client.get_vix_data(days=3)
    vix_level = float(vix_history[-1]["close"]) if vix_history else 0.0

    spy_uptrend = spy_price > spy_ma50
    vix_high = vix_level >= config.VIX_HIGH_THRESHOLD

    if not spy_uptrend and vix_high:
        state = MacroState.NO_TRADE
    elif not spy_uptrend or vix_high:
        state = MacroState.RAISE_BAR
    else:
        state = MacroState.NORMAL

    return MacroSnapshot(
        spy_price=spy_price,
        spy_ma50=spy_ma50,
        vix_level=vix_level,
        spy_uptrend=spy_uptrend,
        vix_high=vix_high,
        state=state,
    )


def rank_candidates(
    candidates: List[Dict],  # list of {technical, catalyst, risk_reward} dicts
    macro: MacroSnapshot,
) -> List[RankedCandidate]:
    """Step 6 — filter by macro gate and rank by reward:risk."""
    if macro.state == MacroState.NO_TRADE:
        return []

    ranked = []
    for c in candidates:
        tech: TechnicalSignal = c["technical"]
        cat: CatalystResult = c["catalyst"]
        rr: RiskRewardResult = c["risk_reward"]

        if macro.state == MacroState.RAISE_BAR:
            # All four signals must be present (rsi_bounce excluded — never fires in uptrend)
            if not (tech.is_uptrend and tech.rsi_momentum
                    and tech.volume_confirmed
                    and tech.support_level is not None):
                continue

        ranked.append(RankedCandidate(
            symbol=tech.symbol,
            technical=tech,
            catalyst=cat,
            risk_reward=rr,
            macro=macro,
            rank_score=rr.reward_risk_ratio,
        ))

    ranked.sort(key=lambda x: x.rank_score, reverse=True)
    return ranked[:2]  # top 1-2


def run_daily_research(
    client: MarketDataClient,
    wash_sale_checker=None,
    date_str: Optional[str] = None,
    watchlist_manager=None,
) -> DailyResearchReport:
    """Orchestrate all 6 steps and return a complete daily report."""
    from datetime import date as date_cls
    if date_str is None:
        date_str = date_cls.today().isoformat()

    error_log: List[str] = []
    near_misses: List[Dict] = []
    earnings_excluded: List[str] = []
    liquidity_excluded: List[str] = []
    passing_candidates: List[Dict] = []

    # Step 5 — macro check first (can short-circuit everything)
    try:
        macro = macro_sentiment_check(client)
    except Exception as e:
        macro = MacroSnapshot(0, 0, 0, False, False, MacroState.NO_TRADE)
        error_log.append(f"macro_sentiment_check error: {e}")

    # Steps 1-4 per symbol
    for symbol in scan_universe(watchlist_manager):
        try:
            tech = technical_screen(symbol, client)
        except Exception as e:
            error_log.append(f"{symbol} technical_screen error: {e}")
            continue

        if not tech.passes_screen:
            above_ma50_pct = round((tech.current_price / tech.ma50 - 1) * 100, 1) if tech.ma50 > 0 else None
            support_pct    = round((tech.current_price - tech.support_level) / tech.current_price * 100, 1) if tech.support_level else None
            criteria_passed = sum([tech.is_uptrend, tech.rsi_momentum, tech.volume_confirmed, tech.support_level is not None])
            near_misses.append({
                "symbol":         symbol,
                "reason":         f"technical: {tech.exclusion_reason}",
                "rsi":            round(tech.rsi, 1),
                "above_ma50_pct": above_ma50_pct,
                "support_pct":    support_pct,
                "criteria_passed": criteria_passed,
            })
            continue

        try:
            cat = catalyst_check(symbol, client)
        except Exception as e:
            error_log.append(f"{symbol} catalyst_check error: {e}")
            continue

        if cat.excluded:
            if "Earnings" in cat.exclusion_reason:
                earnings_excluded.append(symbol)
            else:
                liquidity_excluded.append(symbol)
            near_misses.append({"symbol": symbol, "reason": f"catalyst: {cat.exclusion_reason}"})
            continue

        rr = risk_reward_calc(symbol, tech)
        if not rr.passes:
            near_misses.append({"symbol": symbol, "reason": f"risk_reward: {rr.exclusion_reason}"})
            continue

        passing_candidates.append({"technical": tech, "catalyst": cat, "risk_reward": rr})

    ranked = rank_candidates(passing_candidates, macro)

    # Annotate wash-sale flags
    if wash_sale_checker is not None:
        for candidate in ranked:
            triggered, days = wash_sale_checker(candidate.symbol)
            candidate.wash_sale_flag = triggered
            candidate.wash_sale_days_remaining = days

    return DailyResearchReport(
        date=date_str,
        macro=macro,
        ranked_candidates=ranked,
        near_misses=near_misses,
        earnings_excluded=earnings_excluded,
        liquidity_excluded=liquidity_excluded,
        error_log=error_log,
    )
