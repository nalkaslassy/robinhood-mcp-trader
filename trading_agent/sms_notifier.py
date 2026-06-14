"""
WhatsApp Notifier — sends alerts and trade proposals via Twilio WhatsApp sandbox.

Setup (one-time):
  1. In Twilio console: Messaging -> Try it out -> Send a WhatsApp message
  2. Send the "join <word-word>" code to whatsapp:+14155238886 from your phone
  3. Set TWILIO_WHATSAPP_FROM=whatsapp:+14155238886 in .env (already done)

Approval flow: agent sends you a WhatsApp message, then waits for you to
type YES or NO in the terminal (the run.bat window). Simple and reliable.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from trading_agent import config
from trading_agent.trade_proposal import TradeProposal

logger = logging.getLogger(__name__)

_APPROVAL_WORDS = {"yes", "y", "approve", "ok", "go", "do it"}


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
        else:
            logger.warning("WhatsApp not configured — proposals will appear in terminal only.")

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def _send(self, body: str) -> bool:
        if not self._enabled:
            print(f"\n[WHATSAPP DISABLED] {body}")
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
    # Approval gate
    # ------------------------------------------------------------------

    def wait_for_approval(self, proposal: TradeProposal, **_) -> bool:
        """Send WhatsApp notification, then wait for YES/NO in terminal."""
        self.send_proposal(proposal)
        return self._stdin_approval(proposal)

    # ------------------------------------------------------------------
    # Helpers
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
            "Type YES or NO in the terminal to approve."
        )

    @staticmethod
    def _stdin_approval(proposal: TradeProposal) -> bool:
        print(f"\n{'='*50}")
        print(f"TRADE PROPOSAL: {proposal.symbol}")
        print(f"  Entry:  ${proposal.entry_price_low:.2f} - ${proposal.entry_price_high:.2f}")
        print(f"  Stop:   ${proposal.stop_price:.2f}  ({proposal.stop_pct:.1%} below entry)")
        print(f"  Target: ${proposal.target_price:.2f}  ({proposal.target_pct:.1%} above entry)")
        print(f"  R:R ratio: {proposal.reward_risk_ratio:.2f}")
        print(f"  Position size: ${proposal.position_size_dollars:.0f}")
        print(f"{'='*50}")
        try:
            answer = input("Approve this trade? [YES/no] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in _APPROVAL_WORDS or answer == ""
