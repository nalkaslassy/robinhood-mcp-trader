"""
Reporting module — generates human-readable daily, weekly, and monthly reports,
and maintains the append-only trade journal.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from trading_agent import config


# ---------------------------------------------------------------------------
# Trade journal entry
# ---------------------------------------------------------------------------

@dataclass
class TradeJournalEntry:
    date: str            # ISO date of entry
    symbol: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    stop_price: float
    target_price: float
    outcome: str         # "stop", "target", "manual", "open"
    setup_note: str      # one-line note on setup type / result

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


def log_trade_journal_entry(
    entry: TradeJournalEntry,
    path: Optional[str] = None,
) -> None:
    """Append a trade journal entry to the JSONL log."""
    log_path = path or config.TRADE_JOURNAL_PATH
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        row = {"record_type": "trade_journal", **asdict(entry)}
        f.write(json.dumps(row) + "\n")


def load_journal_entries(path: Optional[str] = None) -> List[TradeJournalEntry]:
    log_path = path or config.TRADE_JOURNAL_PATH
    entries: List[TradeJournalEntry] = []
    if not os.path.exists(log_path):
        return entries
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            if data.get("record_type") == "trade_journal":
                entries.append(TradeJournalEntry(**{
                    k: v for k, v in data.items() if k != "record_type"
                }))
    return entries


# ---------------------------------------------------------------------------
# Report generators
# ---------------------------------------------------------------------------

def generate_daily_report(
    account_value: float,
    cash: float,
    open_positions: List[Dict],
    closed_today: List[TradeJournalEntry],
    proposals: List[Dict],           # list of {proposal, status: "proposed"/"approved"/"expired"/"not_taken"}
    near_misses: List[Dict],
    macro_summary: str,
    earnings_excluded: List[str],
    drawdown_pct: float,
    report_date: Optional[str] = None,
) -> str:
    report_date = report_date or date.today().isoformat()
    lines = [
        f"{'=' * 60}",
        f"  DAILY REPORT — {report_date}",
        f"{'=' * 60}",
        f"  Account value : ${account_value:,.2f}",
        f"  Cash          : ${cash:,.2f}",
        f"  Drawdown      : {drawdown_pct:.1%} from peak",
        f"  Macro         : {macro_summary}",
        "",
    ]

    # Open positions
    lines.append("  OPEN POSITIONS:")
    if open_positions:
        for pos in open_positions:
            pnl_pct = pos.get("unrealized_pnl_pct", 0)
            lines.append(
                f"    {pos['symbol']:8s}  qty={pos['quantity']:.4f}  "
                f"avg=${pos['avg_price']:.2f}  current=${pos['current_price']:.2f}  "
                f"P&L: {pnl_pct:+.1%}"
            )
    else:
        lines.append("    (none)")

    # Closed today
    lines.append("")
    lines.append("  CLOSED TODAY:")
    if closed_today:
        for t in closed_today:
            lines.append(
                f"    {t.symbol:8s}  entry=${t.entry_price:.2f}  "
                f"exit=${t.exit_price:.2f}  P&L: ${t.pnl:+.2f} ({t.pnl_pct:+.1%})  [{t.outcome}]"
            )
    else:
        lines.append("    (none)")

    # Proposals
    lines.append("")
    lines.append("  TRADE PROPOSALS:")
    if proposals:
        for p in proposals:
            prop = p["proposal"]
            status = p["status"]
            lines.append(
                f"    {prop['symbol']:8s}  entry=${prop['entry_low']:.2f}-${prop['entry_high']:.2f}  "
                f"stop=${prop['stop']:.2f}  target=${prop['target']:.2f}  [{status.upper()}]"
            )
    else:
        lines.append("    (none)")

    # Near-misses
    lines.append("")
    lines.append("  NEAR-MISSES (failed screening):")
    if near_misses:
        for nm in near_misses:
            lines.append(f"    {nm['symbol']:8s}  — {nm['reason']}")
    else:
        lines.append("    (none)")

    # Earnings exclusions
    if earnings_excluded:
        lines.append("")
        lines.append(f"  EARNINGS EXCLUDED: {', '.join(earnings_excluded)}")

    lines.append(f"{'=' * 60}")
    return "\n".join(lines)


def generate_weekly_report(
    entries: List[TradeJournalEntry],
    start_date: str,
    end_date: str,
    account_value_start: float,
    account_value_end: float,
    peak_value: float,
    macro_context: str = "",
) -> str:
    closed = [e for e in entries if start_date <= e.date <= end_date and e.outcome != "open"]
    wins = [e for e in closed if e.is_win]
    losses = [e for e in closed if not e.is_win]
    total_pnl = sum(e.pnl for e in closed)
    win_rate = len(wins) / len(closed) if closed else 0.0
    drawdown_pct = (peak_value - account_value_end) / peak_value if peak_value > 0 else 0.0
    week_return = (account_value_end - account_value_start) / account_value_start if account_value_start > 0 else 0.0

    lines = [
        f"{'=' * 60}",
        f"  WEEKLY REPORT — {start_date} to {end_date}",
        f"{'=' * 60}",
        f"  Account value : ${account_value_end:,.2f}  (was ${account_value_start:,.2f})",
        f"  Week return   : {week_return:+.1%}",
        f"  vs. $250 start: {(account_value_end - config.STARTING_CAPITAL) / config.STARTING_CAPITAL:+.1%}",
        f"  Drawdown      : {drawdown_pct:.1%} from peak ${peak_value:,.2f}",
        "",
        f"  Trades this week: {len(closed)}  |  Wins: {len(wins)}  |  Losses: {len(losses)}",
        f"  Win rate      : {win_rate:.0%}",
        f"  Realized P&L  : ${total_pnl:+.2f}",
    ]
    if closed:
        avg_win = sum(e.pnl for e in wins) / len(wins) if wins else 0.0
        avg_loss = sum(e.pnl for e in losses) / len(losses) if losses else 0.0
        lines.append(f"  Avg win       : ${avg_win:+.2f}")
        lines.append(f"  Avg loss      : ${avg_loss:+.2f}")
    if macro_context:
        lines.append("")
        lines.append(f"  Macro context : {macro_context}")
    lines.append(f"{'=' * 60}")
    return "\n".join(lines)


def generate_monthly_report(
    entries: List[TradeJournalEntry],
    month_str: str,          # "2026-06"
    account_value_start: float,
    account_value_end: float,
    spy_return_pct: float,   # SPY total return over same period
    peak_value: float,
    drawdown_history: Optional[List[Dict]] = None,
) -> str:
    closed = [e for e in entries if e.date.startswith(month_str) and e.outcome != "open"]
    wins = [e for e in closed if e.is_win]
    losses = [e for e in closed if not e.is_win]

    win_rate = len(wins) / len(closed) if closed else 0.0
    avg_win = sum(e.pnl for e in wins) / len(wins) if wins else 0.0
    avg_loss = sum(e.pnl for e in losses) / len(losses) if losses else 0.0
    total_pnl = sum(e.pnl for e in closed)
    month_return = (account_value_end - account_value_start) / account_value_start if account_value_start > 0 else 0.0
    vs_start = (account_value_end - config.STARTING_CAPITAL) / config.STARTING_CAPITAL
    alpha = month_return - spy_return_pct
    drawdown_pct = (peak_value - account_value_end) / peak_value if peak_value > 0 else 0.0

    lines = [
        f"{'=' * 60}",
        f"  MONTHLY REPORT — {month_str}",
        f"{'=' * 60}",
        f"  Account value : ${account_value_end:,.2f}  (was ${account_value_start:,.2f})",
        f"  Month return  : {month_return:+.1%}",
        f"  SPY return    : {spy_return_pct:+.1%}",
        f"  Alpha         : {alpha:+.1%}",
        f"  vs. $250 start: {vs_start:+.1%}",
        f"  Max drawdown  : {drawdown_pct:.1%} from peak ${peak_value:,.2f}",
        "",
        f"  Trades this month : {len(closed)}",
        f"  Win rate          : {win_rate:.0%}",
        f"  Avg win           : ${avg_win:+.2f}",
        f"  Avg loss          : ${avg_loss:+.2f}",
        f"  Realized P&L      : ${total_pnl:+.2f}",
        "",
        "  Trade journal summary:",
    ]
    for e in closed:
        lines.append(
            f"    {e.date}  {e.symbol:8s}  ${e.pnl:+.2f} ({e.pnl_pct:+.1%})  [{e.outcome}]  {e.setup_note}"
        )
    if not closed:
        lines.append("    (no closed trades)")

    # Stage recommendation
    lines.append("")
    if win_rate >= 0.5 and month_return > 0:
        lines.append("  Recommendation: Continue Stage 1 — performance on track.")
    elif win_rate < 0.4 or month_return < -0.10:
        lines.append("  Recommendation: PAUSE — review strategy rules before next month.")
    else:
        lines.append("  Recommendation: Continue with review — results mixed, monitor closely.")

    lines.append(f"{'=' * 60}")
    return "\n".join(lines)
