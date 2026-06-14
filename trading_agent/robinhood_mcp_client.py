"""
Robinhood MCP Client — live implementation using the Anthropic SDK.

Claude Haiku handles all MCP tool calls (cheap, fast).
This module implements both:
  - MarketDataClient  (feeds the research engine)
  - MCPOrderClient    (feeds the order executor)

Authentication: the ROBINHOOD_MCP_TOKEN env var holds the OAuth access
token obtained by running setup_auth.py once.

Bracket orders: Robinhood MCP has no native OCO. We place three
separate orders (entry limit-buy, stop-market sell GTC, limit sell GTC)
and the monitoring cycle cancels whichever one didn't fill.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic

from trading_agent import config

logger = logging.getLogger(__name__)

_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
_MCP_SERVER_NAME  = "robinhood-trading"


# ---------------------------------------------------------------------------
# Token resolution — prefers live credentials.json, falls back to .env
# ---------------------------------------------------------------------------

def _get_token() -> str:
    """
    Read the Robinhood MCP access token.
    Tries ~/.claude/.credentials.json first (kept fresh by Claude Code),
    then falls back to the ROBINHOOD_MCP_TOKEN env var.
    """
    if _CREDENTIALS_PATH.exists():
        try:
            creds = json.loads(_CREDENTIALS_PATH.read_text(encoding="utf-8"))
            mcp_oauth = creds.get("mcpOAuth", {})
            for key, entry in mcp_oauth.items():
                if key.startswith(_MCP_SERVER_NAME):
                    token = entry.get("accessToken", "")
                    expires_at = entry.get("expiresAt", 0)
                    # expiresAt is in milliseconds
                    if token and expires_at > time.time() * 1000:
                        return token
                    if token:
                        logger.warning(
                            "Robinhood token in credentials.json is expired. "
                            "Run `claude /mcp` → robinhood-trading → Reconnect to refresh."
                        )
        except Exception as e:
            logger.debug("Could not read credentials.json: %s", e)

    token = os.environ.get("ROBINHOOD_MCP_TOKEN", "")
    if not token or token == "your_robinhood_mcp_token_here":
        raise EnvironmentError(
            "No valid Robinhood MCP token found.\n"
            "Run: claude → /mcp → robinhood-trading → Reconnect"
        )
    return token


def _build_mcp_config() -> Dict:
    return {
        "type": "url",
        "url": "https://agent.robinhood.com/mcp/trading",
        "name": "robinhood",
        "authorization_token": _get_token(),
    }


def _call_robinhood(
    claude: anthropic.Anthropic,
    instruction: str,
    *,
    model: str = config.MODEL_DATA,
    max_tokens: int = 8192,
) -> str:
    """
    Send a single-turn instruction to Claude Haiku with the Robinhood MCP
    server attached. Returns the final text response after all tool calls
    complete.

    Claude will autonomously call the appropriate Robinhood MCP tools and
    return a structured result. We ask it to always respond with JSON so
    we can parse the output cleanly.
    """
    response = claude.beta.messages.create(
        model=model,
        max_tokens=max_tokens,
        betas=["mcp-client-2025-04-04"],
        mcp_servers=[_build_mcp_config()],
        messages=[
            {
                "role": "user",
                "content": (
                    f"{instruction}\n\n"
                    "Respond with ONLY a valid JSON object or array — "
                    "no explanation, no markdown fences."
                ),
            }
        ],
    )
    # Extract the last text block from the response
    for block in reversed(response.content):
        if hasattr(block, "text"):
            return block.text.strip()
    return "{}"


# ---------------------------------------------------------------------------
# Market Data Client (feeds research_engine.py)
# ---------------------------------------------------------------------------

class RobinhoodMarketDataClient:
    """
    Implements the MarketDataClient protocol from research_engine.py.
    Uses Claude Haiku + Robinhood MCP for all data fetching.
    """

    def __init__(self, claude: Optional[anthropic.Anthropic] = None):
        self._claude = claude or anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"]
        )

    def get_price_history(self, symbol: str, days: int) -> List[Dict]:
        """
        Fetch daily OHLCV bars for `symbol` going back `days` calendar days.
        Maps to get_equity_historicals (stocks) or uses VXX as VIX proxy.
        """
        # VIX historical data is not available directly; use VXX as proxy
        fetch_symbol = "VXX" if symbol == "VIX" else symbol

        raw = _call_robinhood(
            self._claude,
            f"Use get_equity_historicals to fetch daily OHLCV data for "
            f"{fetch_symbol} for the past {days} calendar days. "
            f"Return a JSON array where each element has keys: "
            f"date, open, high, low, close, volume. "
            f"Sort oldest first. Include only complete trading days.",
        )
        try:
            bars = json.loads(raw)
            if not isinstance(bars, list):
                bars = bars.get("results", bars.get("data", []))
            return bars
        except Exception as e:
            logger.error("get_price_history(%s) parse error: %s | raw: %.200s", symbol, e, raw)
            return []

    def get_current_price(self, symbol: str) -> float:
        raw = _call_robinhood(
            self._claude,
            f"Use get_equity_quotes to get the current price for {symbol}. "
            f'Return JSON: {{"price": <number>}}',
        )
        try:
            return float(json.loads(raw).get("price", 0.0))
        except Exception:
            return 0.0

    def get_upcoming_earnings(self, symbol: str) -> Optional[str]:
        """
        Robinhood MCP does not have a dedicated earnings endpoint.
        We ask Claude to use whatever data is available.
        Returns ISO date string or None.
        """
        raw = _call_robinhood(
            self._claude,
            f"Use get_equity_tradability or any available tool to check if "
            f"{symbol} has an upcoming earnings date. "
            f'Return JSON: {{"earnings_date": "YYYY-MM-DD"}} or {{"earnings_date": null}}',
        )
        try:
            val = json.loads(raw).get("earnings_date")
            return val if val else None
        except Exception:
            return None

    def get_recent_news(self, symbol: str, days: int) -> List[Dict]:
        # Robinhood MCP does not expose a news feed — return empty
        return []

    def get_vix_data(self, days: int) -> List[Dict]:
        """
        Fetch current VIX level via get_index_quotes (real-time).
        For historical VIX bars, use VXX equity historicals as a proxy.
        """
        raw = _call_robinhood(
            self._claude,
            "Use get_index_quotes to get the current VIX level. "
            'Return JSON: {"close": <vix_level>, "date": "YYYY-MM-DD"}',
        )
        try:
            bar = json.loads(raw)
            return [bar]
        except Exception as e:
            logger.error("get_vix_data parse error: %s", e)
            return [{"close": 20.0, "date": date.today().isoformat()}]

    def get_vix_open(self) -> float:
        """
        Get today's VIX open for intraday spike detection.
        Falls back to VXX open price if direct VIX open unavailable.
        """
        raw = _call_robinhood(
            self._claude,
            "Use get_equity_historicals on VXX to get today's open price. "
            'Return JSON: {"open": <number>}',
        )
        try:
            return float(json.loads(raw).get("open", 20.0))
        except Exception:
            return 20.0

    def get_popular_watchlist_symbols(self) -> List[str]:
        """
        Pull symbols from Robinhood's popular watchlists for the
        WatchlistManager to evaluate as potential additions.
        """
        raw = _call_robinhood(
            self._claude,
            "Use get_popular_watchlists to get the names of popular watchlists, "
            "then use get_watchlist_items to get the symbols in the top 3 lists. "
            "Return a flat JSON array of unique ticker symbols, e.g. "
            '["AAPL", "NVDA", "MSFT"]',
        )
        try:
            symbols = json.loads(raw)
            if isinstance(symbols, list):
                return [str(s).upper() for s in symbols]
            return []
        except Exception as e:
            logger.error("get_popular_watchlist_symbols error: %s", e)
            return []


# ---------------------------------------------------------------------------
# Order Client (feeds order_executor.py)
# ---------------------------------------------------------------------------

class RobinhoodOrderClient:
    """
    Implements the MCPOrderClient protocol from order_executor.py.
    All order operations use Claude Haiku + Robinhood MCP.
    """

    def __init__(self, claude: Optional[anthropic.Anthropic] = None):
        self._claude = claude or anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"]
        )
        self._account_number: Optional[str] = None

    def _get_account_number(self) -> str:
        if self._account_number:
            return self._account_number
        raw = _call_robinhood(
            self._claude,
            "Use get_accounts to find the account where agentic_allowed is true. "
            'Return JSON: {"account_number": "<number>"}',
        )
        try:
            self._account_number = json.loads(raw)["account_number"]
            return self._account_number
        except Exception as e:
            raise RuntimeError(f"Could not determine agentic account number: {e}")

    def get_account(self) -> Dict:
        acct_num = self._get_account_number()
        raw = _call_robinhood(
            self._claude,
            f"Use get_portfolio for account {acct_num}. "
            "Return JSON with keys: cash, buying_power, total_value. "
            "Also use get_equity_positions for this account and include a "
            '"positions" array where each element has: symbol, quantity, '
            "average_buy_price, current_price.",
        )
        try:
            return json.loads(raw)
        except Exception:
            return {"cash": 0.0, "buying_power": 0.0, "total_value": 0.0, "positions": []}

    def get_positions(self) -> List[Dict]:
        data = self.get_account()
        return data.get("positions", [])

    def get_open_orders(self) -> List[Dict]:
        acct_num = self._get_account_number()
        raw = _call_robinhood(
            self._claude,
            f"Use get_equity_orders to get all open orders for account {acct_num}. "
            "Return a JSON array where each element has: id, symbol, type, "
            "side, quantity, price, stop_price, state.",
        )
        try:
            orders = json.loads(raw)
            return orders if isinstance(orders, list) else []
        except Exception:
            return []

    def get_current_price(self, symbol: str) -> float:
        raw = _call_robinhood(
            self._claude,
            f"Use get_equity_quotes to get the current price for {symbol}. "
            'Return JSON: {"price": <number>}',
        )
        try:
            return float(json.loads(raw).get("price", 0.0))
        except Exception:
            return 0.0

    def place_limit_buy(self, symbol: str, quantity: float, limit_price: float) -> Dict:
        acct_num = self._get_account_number()
        raw = _call_robinhood(
            self._claude,
            f"Use place_equity_order to place a LIMIT BUY order: "
            f"account_number={acct_num}, symbol={symbol}, "
            f"side=buy, type=limit, quantity={quantity:.6f}, "
            f"limit_price={limit_price:.4f}, time_in_force=gtc. "
            'Return JSON: {"id": "<order_id>", "state": "<state>"}',
        )
        try:
            return json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"place_limit_buy failed: {e}")

    def place_stop_loss(self, symbol: str, quantity: float, stop_price: float) -> Dict:
        acct_num = self._get_account_number()
        raw = _call_robinhood(
            self._claude,
            f"Use place_equity_order to place a STOP MARKET SELL order: "
            f"account_number={acct_num}, symbol={symbol}, "
            f"side=sell, type=stop_market, quantity={quantity:.6f}, "
            f"stop_price={stop_price:.4f}, time_in_force=gtc. "
            'Return JSON: {"id": "<order_id>", "state": "<state>"}',
        )
        try:
            return json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"place_stop_loss failed: {e}")

    def place_limit_sell(self, symbol: str, quantity: float, limit_price: float) -> Dict:
        acct_num = self._get_account_number()
        raw = _call_robinhood(
            self._claude,
            f"Use place_equity_order to place a LIMIT SELL order: "
            f"account_number={acct_num}, symbol={symbol}, "
            f"side=sell, type=limit, quantity={quantity:.6f}, "
            f"limit_price={limit_price:.4f}, time_in_force=gtc. "
            'Return JSON: {"id": "<order_id>", "state": "<state>"}',
        )
        try:
            return json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"place_limit_sell failed: {e}")

    def cancel_order(self, order_id: str) -> Dict:
        raw = _call_robinhood(
            self._claude,
            f"Use cancel_equity_order to cancel order id={order_id}. "
            'Return JSON: {"status": "cancelled"}',
        )
        try:
            return json.loads(raw)
        except Exception:
            return {"status": "unknown"}
