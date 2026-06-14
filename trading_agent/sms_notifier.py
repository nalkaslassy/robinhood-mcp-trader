"""
SMS Notifier — Twilio-based approval flow.

Sends a trade proposal to your phone as an SMS.
You reply YES (or Y) to approve, anything else to reject.

The notifier polls for your reply for up to ENTRY_RECOMMENDATION_EXPIRY_HOURS.
If no reply arrives before expiry, the proposal is automatically discarded.

Setup:
  1. Create a free Twilio account at twilio.com
  2. Get a Twilio phone number (free trial number works)
  3. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, YOUR_PHONE_NUMBER in .env
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from trading_agent import config
from trading_agent.trade_proposal import TradeProposal

logger = logging.getLogger(__name__)

_APPROVAL_WORDS = {"yes", "y", "approve", "ok", "go", "do it"}
_REJECTION_WORDS = {"no", "n", "reject", "skip", "pass", "stop"}


class SMSNotifier:
    def __init__(self):
        self._enabled = self._check_env()
        if self._enabled:
            from twilio.rest import Client as TwilioClient
            self._client = TwilioClient(
                os.environ["TWILIO_ACCOUNT_SID"],
                os.environ["TWILIO_AUTH_TOKEN"],
            )
        self._from_number  = os.environ.get("TWILIO_FROM", "")
        self._to_number    = os.environ.get("YOUR_PHONE_NUMBER", "")

    def _check_env(self) -> bool:
        required = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "YOUR_PHONE_NUMBER"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            logger.warning("Twilio not configured (%s missing) — falling back to stdin approval", missing)
            return False
        return True

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def send_proposal(self, proposal: TradeProposal) -> bool:
        """Send the trade proposal as an SMS. Returns True if sent successfully."""
        body = self._format_proposal_sms(proposal)
        if not self._enabled:
            print("\n[SMS DISABLED] Would have sent:\n" + body)
            return False
        try:
            msg = self._client.messages.create(
                body=body,
                from_=self._from_number,
                to=self._to_number,
            )
            logger.info("Proposal SMS sent: SID=%s", msg.sid)
            return True
        except Exception as e:
            logger.error("Failed to send proposal SMS: %s", e)
            return False

    def send_confirmation(self, symbol: str, order_id: str, dry_run: bool = False) -> None:
        tag = "[DRY RUN] " if dry_run else ""
        body = (
            f"{tag}✓ TRADE PLACED: {symbol}\n"
            f"Entry order: {order_id}\n"
            "Stop-loss and profit-target orders are live."
        )
        self._send_text(body)

    def send_alert(self, message: str) -> None:
        """Send an arbitrary alert (emergency, drawdown breaker, etc.)."""
        self._send_text(f"⚠️ TRADING ALERT\n{message}")

    def _send_text(self, body: str) -> None:
        if not self._enabled:
            print(f"\n[SMS DISABLED] {body}")
            return
        try:
            self._client.messages.create(
                body=body, from_=self._from_number, to=self._to_number
            )
        except Exception as e:
            logger.error("SMS send error: %s", e)

    # ------------------------------------------------------------------
    # Wait for reply (approval gate)
    # ------------------------------------------------------------------

    def wait_for_approval(
        self,
        proposal: TradeProposal,
        poll_interval_seconds: int = 30,
    ) -> bool:
        """
        Poll for an inbound SMS reply until the proposal expires.
        Returns True if you replied YES, False if rejected or expired.
        """
        if not self._enabled:
            return self._stdin_fallback(proposal)

        logger.info(
            "Waiting for SMS approval for %s (expires %s)",
            proposal.symbol,
            proposal.expiration.strftime("%H:%M UTC"),
        )

        while not proposal.is_expired():
            reply = self._get_latest_reply()
            if reply is not None:
                reply_clean = reply.strip().lower()
                if reply_clean in _APPROVAL_WORDS:
                    logger.info("SMS approval received for %s", proposal.symbol)
                    return True
                if reply_clean in _REJECTION_WORDS:
                    logger.info("SMS rejection received for %s", proposal.symbol)
                    return False
                # Ambiguous reply — send clarification
                self._send_text(
                    f"Reply YES to approve {proposal.symbol} or NO to skip."
                )
            time.sleep(poll_interval_seconds)

        logger.info("Proposal for %s expired without approval", proposal.symbol)
        self.send_alert(f"Proposal for {proposal.symbol} expired — no action taken.")
        return False

    def _get_latest_reply(self) -> Optional[str]:
        """Return the body of the most recent inbound SMS, or None."""
        try:
            messages = self._client.messages.list(
                to=self._from_number,  # messages sent TO our Twilio number
                limit=1,
            )
            if messages:
                return messages[0].body
            return None
        except Exception as e:
            logger.error("Failed to fetch inbound SMS: %s", e)
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_proposal_sms(proposal: TradeProposal) -> str:
        expiry_str = proposal.expiration.strftime("%I:%M %p UTC")
        lev_tag = " [LEVERAGED ETF]" if proposal.is_leveraged_etf else ""
        ws_tag  = f" ⚠️ WASH SALE: {proposal.wash_sale_days_remaining}d" if proposal.wash_sale_flag else ""
        return (
            f"📊 TRADE PROPOSAL: {proposal.symbol}{lev_tag}{ws_tag}\n"
            f"Entry:  ${proposal.entry_price_low:.2f}–${proposal.entry_price_high:.2f}\n"
            f"Stop:   ${proposal.stop_price:.2f} ({proposal.stop_pct:.1%} risk)\n"
            f"Target: ${proposal.target_price:.2f} ({proposal.target_pct:.1%})\n"
            f"R:R = {proposal.reward_risk_ratio:.2f} | Size: ${proposal.position_size_dollars:.2f}\n"
            f"Expires: {expiry_str}\n"
            f"Reply YES to approve or NO to skip."
        )

    @staticmethod
    def _stdin_fallback(proposal: TradeProposal) -> bool:
        """Used when Twilio is not configured (dev/dry-run mode)."""
        print(f"\n[STDIN APPROVAL] {proposal.symbol} — Reply YES or NO")
        try:
            answer = input("Approve? [y/N] ").strip().lower()
            return answer in _APPROVAL_WORDS
        except (EOFError, KeyboardInterrupt):
            return False
