"""
Watchlist Manager — the research agent decides what's on the list.

Every trading day: records how each symbol performed in research
  (qualified setup / near-miss / hard-excluded / failed screen).

Every Friday: calls Claude Sonnet to review the week's data and make
  decisions: which symbols to keep, remove, or add. The model's
  reasoning is logged alongside every decision so the choices are
  transparent and auditable.

The human never edits the watchlist directly — the research quality
record is the only input to the decision.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

from trading_agent import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SymbolRecord:
    symbol: str
    status: str            # "active" | "candidate" | "removed"
    added_date: str        # ISO date first added to active list
    last_reviewed: str     # ISO date of last weekly review
    consecutive_fails: int = 0   # research days with zero setup signal
    total_near_misses: int = 0
    total_qualified: int = 0
    removal_reason: str = ""
    review_after: str = ""       # ISO date — when to re-evaluate if removed


@dataclass
class WatchlistState:
    records: Dict[str, SymbolRecord] = field(default_factory=dict)
    last_weekly_review: str = ""
    review_log: List[Dict] = field(default_factory=list)  # audit trail

    def active_symbols(self) -> List[str]:
        return sorted(
            s for s, r in self.records.items() if r.status == "active"
        )

    def candidate_symbols(self) -> List[str]:
        return sorted(
            s for s, r in self.records.items() if r.status == "candidate"
        )


# ---------------------------------------------------------------------------
# Outcome codes recorded after each daily research run
# ---------------------------------------------------------------------------
OUTCOME_QUALIFIED   = "qualified"    # passed all 6 steps — became a proposal
OUTCOME_NEAR_MISS   = "near_miss"    # passed most steps but one gate blocked it
OUTCOME_TECH_FAIL   = "tech_fail"    # failed technical screen (step 2)
OUTCOME_EXCLUDED    = "excluded"     # hard-excluded (earnings / liquidity)
OUTCOME_MACRO_STOP  = "macro_stop"   # macro NO_TRADE state — not evaluated


# ---------------------------------------------------------------------------
# Manager class
# ---------------------------------------------------------------------------

class WatchlistManager:
    def __init__(
        self,
        path: Optional[str] = None,
        anthropic_client=None,
    ):
        self._path = path or config.WATCHLIST_STATE_PATH
        self._claude = anthropic_client
        self._state = self._load()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _load(self) -> WatchlistState:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                records = {
                    sym: SymbolRecord(**rec)
                    for sym, rec in raw.get("records", {}).items()
                }
                return WatchlistState(
                    records=records,
                    last_weekly_review=raw.get("last_weekly_review", ""),
                    review_log=raw.get("review_log", []),
                )
            except Exception as e:
                logger.warning("Failed to load watchlist state: %s — seeding from config", e)

        return self._seed_from_config()

    def _seed_from_config(self) -> WatchlistState:
        """First-run: populate from config.WATCHLIST_SEED."""
        today = date.today().isoformat()
        records = {
            sym: SymbolRecord(
                symbol=sym,
                status="active",
                added_date=today,
                last_reviewed=today,
            )
            for sym in config.WATCHLIST_SEED
        }
        state = WatchlistState(records=records)
        self._save(state)
        logger.info("Watchlist seeded with %d symbols from config", len(records))
        return state

    def _save(self, state: Optional[WatchlistState] = None):
        s = state or self._state
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        payload = {
            "records": {sym: asdict(rec) for sym, rec in s.records.items()},
            "last_weekly_review": s.last_weekly_review,
            "review_log": s.review_log,
        }
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    # ------------------------------------------------------------------
    # Public queries
    # ------------------------------------------------------------------

    def get_active_symbols(self) -> List[str]:
        symbols = self._state.active_symbols()
        # Permanent symbols are always included
        for sym in config.WATCHLIST_PERMANENT:
            if sym not in symbols:
                symbols.append(sym)
        return sorted(symbols)

    def get_record(self, symbol: str) -> Optional[SymbolRecord]:
        return self._state.records.get(symbol)

    # ------------------------------------------------------------------
    # Daily outcome recording
    # ------------------------------------------------------------------

    def record_daily_outcomes(self, report) -> None:
        """
        Called after each daily research run.
        `report` is a DailyResearchReport from research_engine.
        """
        today = date.today().isoformat()
        macro_no_trade = report.macro.state.value == "NO_TRADE"

        qualified_symbols = {c.symbol for c in report.ranked_candidates}
        near_miss_symbols = {nm["symbol"] for nm in report.near_misses}
        excluded_symbols  = set(report.earnings_excluded + report.liquidity_excluded)

        for symbol, rec in self._state.records.items():
            if rec.status != "active":
                continue

            if macro_no_trade:
                outcome = OUTCOME_MACRO_STOP
            elif symbol in qualified_symbols:
                outcome = OUTCOME_QUALIFIED
                rec.total_qualified += 1
                rec.consecutive_fails = 0
            elif symbol in near_miss_symbols:
                outcome = OUTCOME_NEAR_MISS
                rec.total_near_misses += 1
                rec.consecutive_fails = 0  # near-miss resets the counter
            elif symbol in excluded_symbols:
                outcome = OUTCOME_EXCLUDED
                # Earnings/liquidity exclusions don't count as "fails"
            else:
                outcome = OUTCOME_TECH_FAIL
                rec.consecutive_fails += 1

            logger.debug("%s: %s (consecutive_fails=%d)", symbol, outcome, rec.consecutive_fails)

        self._save()

    # ------------------------------------------------------------------
    # Candidate registration (from Robinhood popular lists or other sources)
    # ------------------------------------------------------------------

    def register_candidates(self, symbols: List[str]) -> None:
        """Add new symbols as 'candidate' status for the weekly review to evaluate."""
        today = date.today().isoformat()
        added = []
        for sym in symbols:
            sym = sym.upper().strip()
            if sym not in self._state.records:
                self._state.records[sym] = SymbolRecord(
                    symbol=sym,
                    status="candidate",
                    added_date=today,
                    last_reviewed=today,
                )
                added.append(sym)
        if added:
            logger.info("Registered %d new candidates: %s", len(added), added)
            self._save()

    # ------------------------------------------------------------------
    # Weekly review — Claude Sonnet makes the decisions
    # ------------------------------------------------------------------

    def should_run_weekly_review(self) -> bool:
        """True on Fridays, or if review has never run."""
        if not self._state.last_weekly_review:
            return True
        today = date.today()
        last = date.fromisoformat(self._state.last_weekly_review)
        days_since = (today - last).days
        return today.weekday() == 4 or days_since >= 7  # Friday=4

    def run_weekly_review(self, popular_candidates: Optional[List[str]] = None) -> Dict:
        """
        Call Claude Sonnet with the week's research performance data.
        Returns the parsed decision dict and applies changes to state.
        """
        if self._claude is None:
            logger.warning("No Claude client — skipping weekly watchlist review")
            return {}

        summary = self._build_performance_summary()
        candidates_str = ", ".join(popular_candidates or []) or "none"
        today = date.today().isoformat()

        prompt = f"""You are the research director for a disciplined swing-trading system
targeting a small ($250) Robinhood Agentic account. Your job is the weekly
watchlist review: decide which symbols to keep, remove, or add.

PERFORMANCE DATA (past week):
{summary}

CANDIDATES FROM ROBINHOOD TRENDING LISTS (not yet on watchlist):
{candidates_str}

RULES you must follow:
- REMOVE a symbol if consecutive_fails >= {config.WATCHLIST_FAIL_THRESHOLD} AND
  it has never produced a qualified setup (total_qualified == 0).
- REMOVE a symbol if it has been active > 30 days with zero near-misses AND
  zero qualified setups.
- KEEP a symbol if it produced a near-miss or qualified setup this week,
  even if consecutive_fails is high (conditions change).
- These symbols are PERMANENT and must never be removed: {config.WATCHLIST_PERMANENT}.
- ADD a candidate only if it is a well-known, highly liquid stock/ETF
  (large-cap or sector ETF). Do not add speculative micro-caps.
- Maximum {config.WATCHLIST_MAX_ACTIVE} active symbols total.
- Leveraged ETFs (3x) are acceptable but limit additions to ones already
  in the system: {config.LEVERAGED_ETFS}.

Respond with ONLY a valid JSON object in this exact format:
{{
  "keep":   ["SYM1", "SYM2"],
  "remove": [{{"symbol": "SYM", "reason": "one sentence"}}],
  "add":    [{{"symbol": "SYM", "reason": "one sentence"}}],
  "reasoning": "2-3 sentence summary of the overall watchlist health"
}}"""

        try:
            response = self._claude.messages.create(
                model=config.MODEL_DECISIONS,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            decisions = json.loads(raw.strip())
        except Exception as e:
            logger.error("Weekly review Claude call failed: %s", e)
            return {}

        self._apply_decisions(decisions, today)
        self._state.last_weekly_review = today
        self._state.review_log.append({
            "date": today,
            "decisions": decisions,
        })
        self._save()

        logger.info(
            "Weekly review complete: kept=%d removed=%d added=%d",
            len(decisions.get("keep", [])),
            len(decisions.get("remove", [])),
            len(decisions.get("add", [])),
        )
        return decisions

    def _apply_decisions(self, decisions: Dict, today: str) -> None:
        for entry in decisions.get("remove", []):
            sym = entry["symbol"]
            if sym in config.WATCHLIST_PERMANENT:
                continue
            if sym in self._state.records:
                rec = self._state.records[sym]
                rec.status = "removed"
                rec.removal_reason = entry.get("reason", "")
                rec.last_reviewed = today
                # Re-evaluate in 30 days
                review_dt = date.fromisoformat(today) + timedelta(days=30)
                rec.review_after = review_dt.isoformat()
                logger.info("REMOVED %s: %s", sym, rec.removal_reason)

        for entry in decisions.get("add", []):
            sym = entry["symbol"]
            if sym in self._state.records:
                rec = self._state.records[sym]
                if rec.status != "active":
                    rec.status = "active"
                    rec.added_date = today
                    rec.last_reviewed = today
                    rec.consecutive_fails = 0
                    logger.info("RE-ACTIVATED %s: %s", sym, entry.get("reason", ""))
            else:
                self._state.records[sym] = SymbolRecord(
                    symbol=sym,
                    status="active",
                    added_date=today,
                    last_reviewed=today,
                )
                logger.info("ADDED %s: %s", sym, entry.get("reason", ""))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_performance_summary(self) -> str:
        lines = []
        for sym, rec in sorted(self._state.records.items()):
            if rec.status not in ("active", "candidate"):
                continue
            lines.append(
                f"  {sym}: status={rec.status} "
                f"consecutive_fails={rec.consecutive_fails} "
                f"near_misses={rec.total_near_misses} "
                f"qualified={rec.total_qualified} "
                f"added={rec.added_date}"
            )
        return "\n".join(lines) if lines else "  (no records)"
