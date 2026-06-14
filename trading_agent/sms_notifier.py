"""
WhatsApp Notifier — two-way approval via Twilio WhatsApp sandbox.

Flow:
  1. Agent sends you a WhatsApp message with trade details.
  2. You reply YES (or NO) from your phone.
  3. Agent polls Twilio for your reply and places/skips the trade.

Setup (one-time):
  1. In Twilio console: Messaging -> Try it out -> Send a WhatsApp message
  2. Send the "join <word-word>" code to whatsapp:+14155238886 from your phone
  3. Set TWILIO_WHATSAPP_FROM=whatsapp:+14155238886 in .env
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

_APPROVAL_WORDS  = {"yes", "y", "approve", "ok", "go"}
_REJECTION_WORDS = {"no", "n", "reject", "skip", "pass", "stop"}

# How long to wait for a WhatsApp reply before giving up
_APPROVAL_TIMEOUT_MINUTES = 30
_POLL_INTERVAL_SECONDS    = 20


class SMSNotifier:
    def __init__(self):
        self._account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        self._auth_token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
        self._from_wa     = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        phone             = os.environ.get("YOUR_PHONE_NUMBER", "")
        self._to_wa       = f"whatsapp:{phone}" if phone and not phone.startswith("whatsapp:") else phone

        self._enabled = bool(
            self._account_sid and self._auth_token and phone
            and self._account_sid != "your_twilio_account_sid_here"
        )
        if self._enabled:
            from twilio.rest import Client as TwilioClient
            self._client = TwilioClient(self._account_sid, self._auth_token)
            logger.info("WhatsApp notifier ready (%s -> %s)", self._from_wa, self._to_wa)
        else:
            logger.warning("WhatsApp not configured — falling back to terminal approval.")

    # ------------------------------------------------------------------
    # Outbound messages
    # ------------------------------------------------------------------

    def _send(self, body: str) -> bool:
        if not self._enabled:
            print(f"\n[WHATSAPP DISABLED]\n{body}")
            return False
        try:
            msg = self._client.messages.create(
                body=body,
                from_=self._from_wa,
                to=self._to_wa,
            )
            logger.info("WhatsApp sent: SID=%s status=%s", msg.sid, msg.status)
            return True
        except Exception as e:
            logger.error("WhatsApp send error: %s", e)
            return False

    def send_proposal(self, proposal: TradeProposal) -> bool:
        return self._send(self._format_proposal(proposal))

    def send_confirmation(self, symbol: str, order_id: str, dry_run: bool = False) -> None:
        tag = "[DRY RUN] " if dry_run else ""
        self._send(
            f"{tag}TRADE PLACED: {symbol}\n"
            f"Entry order: {order_id}\n"
            "Stop-loss and profit-target orders are live."
        )

    def send_alert(self, message: str) -> None:
        self._send(f"TRADING ALERT\n{message}")

    # ------------------------------------------------------------------
    # Approval gate — send WhatsApp, wait for reply
    # ------------------------------------------------------------------

    def wait_for_approval(self, proposal: TradeProposal, **_) -> bool:
        """
        Send a WhatsApp with trade details, then poll for your reply.
        Returns True if you reply YES within the timeout window.
        Falls back to terminal input if WhatsApp is not configured.
        """
        if not self._enabled:
            return self._stdin_fallback(proposal)

        sent_at = datetime.now(timezone.utc)
        self.send_proposal(proposal)

        deadline = _APPROVAL_TIMEOUT_MINUTES * 60
        elapsed  = 0

        logger.info(
            "Waiting up to %d minutes for WhatsApp reply for %s...",
            _APPROVAL_TIMEOUT_MINUTES, proposal.symbol,
        )

        while elapsed < deadline:
            time.sleep(_POLL_INTERVAL_SECONDS)
            elapsed += _POLL_INTERVAL_SECONDS

            reply = self._get_reply_since(sent_at)
            if reply is None:
                continue

            word = reply.strip().lower()
            if word in _APPROVAL_WORDS:
                logger.info("WhatsApp approval received for %s", proposal.symbol)
                return True
            if word in _REJECTION_WORDS:
                logger.info("WhatsApp rejection received for %s", proposal.symbol)
                return False
            # Unrecognised reply — ask again
            self._send(f"Reply YES to approve {proposal.symbol} or NO to skip.")

        logger.info("No WhatsApp reply received for %s — skipping.", proposal.symbol)
        self.send_alert(f"No reply received for {proposal.symbol} in {_APPROVAL_TIMEOUT_MINUTES} min. Trade skipped.")
        return False

    # ------------------------------------------------------------------
    # Poll for inbound WhatsApp reply
    # ------------------------------------------------------------------

    def _get_reply_since(self, since: datetime) -> Optional[str]:
        """
        Return the body of the most recent WhatsApp message sent FROM the
        user TO our Twilio sandbox number, received after `since`.
        """
        try:
            messages = self._client.messages.list(
                to=self._from_wa,       # messages arriving at our sandbox number
                from_=self._to_wa,      # sent by the user's WhatsApp
                limit=5,
            )
            for msg in messages:
                msg_time = msg.date_created
                # Twilio returns timezone-aware datetimes
                if msg_time.tzinfo is None:
                    msg_time = msg_time.replace(tzinfo=timezone.utc)
                if msg_time > since:
                    return msg.body
        except Exception as e:
            logger.error("Failed to check WhatsApp replies: %s", e)
        return None

    # ------------------------------------------------------------------
    # Terminal fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _format_proposal(proposal: TradeProposal) -> str:
        lev = " [LEVERAGED]" if proposal.is_leveraged_etf else ""
        ws  = f" WASH SALE {proposal.wash_sale_days_remaining}d" if proposal.wash_sale_flag else ""
        return (
            f"TRADE PROPOSAL: {proposal.symbol}{lev}{ws}\n"
            f"Entry:  ${proposal.entry_price_low:.2f} - ${proposal.entry_price_high:.2f}\n"
            f"Stop:   ${proposal.stop_price:.2f} ({proposal.stop_pct:.1%} risk)\n"
            f"Target: ${proposal.target_price:.2f} ({proposal.target_pct:.1%})\n"
            f"R:R = {proposal.reward_risk_ratio:.2f}  Size: ${proposal.position_size_dollars:.0f}\n"
            f"Reply YES or NO within {_APPROVAL_TIMEOUT_MINUTES} minutes."
        )

    @staticmethod
    def _stdin_fallback(proposal: TradeProposal) -> bool:
        print(f"\n{'='*50}")
        print(f"TRADE PROPOSAL: {proposal.symbol}")
        print(f"  Entry:  ${proposal.entry_price_low:.2f} - ${proposal.entry_price_high:.2f}")
        print(f"  Stop:   ${proposal.stop_price:.2f}  ({proposal.stop_pct:.1%} below entry)")
        print(f"  Target: ${proposal.target_price:.2f}  ({proposal.target_pct:.1%} above entry)")
        print(f"  R:R: {proposal.reward_risk_ratio:.2f}   Size: ${proposal.position_size_dollars:.0f}")
        print(f"{'='*50}")
        try:
            answer = input("Approve? [yes/NO] ").strip().lower()
            return answer in _APPROVAL_WORDS
        except (EOFError, KeyboardInterrupt):
            return False
