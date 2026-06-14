# Robinhood MCP Trading Agent

> **DISCLAIMER — READ BEFORE USE**
>
> This project is for **educational and portfolio-demonstration purposes only**.
> It is **not financial advice**. Trading involves substantial risk of loss.
> Past performance does not guarantee future results. **You assume all risk**
> for any live trading you conduct using this software. The author(s) are not
> registered investment advisers. Never risk money you cannot afford to lose.

---

## What it does

A fully-automated Python swing-trading agent for a small Robinhood account. Every weekday morning it:

1. Pulls free market data via **yfinance** (no API cost)
2. Runs a 6-step technical screening pipeline on a 19-symbol watchlist
3. Checks macro conditions (SPY trend + VIX level)
4. If a stock passes all screens → sends you a **WhatsApp message** with the trade details
5. You reply **YES** or **NO** from your phone
6. On YES → places a bracket order on Robinhood (limit buy + stop-loss + profit target)
7. Saves a daily log to `logs/YYYYMMDD.txt`

The agent runs automatically at 9:45 AM Mon–Fri via **Windows Task Scheduler** — no manual action required.

---

## Cost breakdown

| Component | Cost |
|---|---|
| Daily market data (yfinance) | **$0** |
| Weekly watchlist AI review (Claude Sonnet) | ~$0.02 / week |
| Order placement via Robinhood MCP | ~$0.10 / trade |
| WhatsApp notifications (Twilio sandbox) | **$0** |

Running the agent every day costs essentially nothing unless a trade fires.

---

## Position sizing (designed for a $250 account)

All percentages scale automatically to any account size.

| Parameter | Value |
|---|---|
| Position size | 25–30% of account per trade |
| Max concurrent positions | 2 |
| Cash reserve (always kept back) | 15% |
| Stop loss | 5–7% below entry |
| Profit target | 10–20% above entry |
| Minimum reward:risk required | 1.5:1 |
| Drawdown breaker | −15% from peak → pause all new entries |

With a $250 account: each position is ~$68, worst-case loss if both stop out = ~$9.60 (−3.9% of account).

---

## Screening pipeline (6 steps)

1. **Universe scan** — active watchlist from WatchlistManager (starts with 19 symbols)
2. **Technical screen** — price above MA50, RSI bounce or momentum, volume confirmation, support level within 5–7% for stop
3. **Catalyst check** — excludes stocks with earnings within 7 days, or avg daily dollar volume < $50M
4. **Risk/reward calc** — computes stop and target from support/resistance; rejects if R:R < 1.5
5. **Macro gate** — SPY vs MA50 + VIX level → NORMAL / RAISE_BAR / NO_TRADE
6. **Rank** — top 2 candidates by reward:risk ratio

---

## Project structure

```
robinhood-mcp-trader/
├── trading_agent/
│   ├── config.py              # All constants — review before going live
│   ├── yfinance_client.py     # Free market data (replaces expensive MCP data calls)
│   ├── robinhood_mcp_client.py # Robinhood MCP — used only for order placement
│   ├── research_engine.py     # 6-step screening pipeline
│   ├── watchlist_manager.py   # Dynamic watchlist with weekly AI review
│   ├── main_agent.py          # Orchestration + CLI entry point
│   ├── order_executor.py      # Bracket order placement + integrity checks
│   ├── account_state.py       # Cash, positions, drawdown tracking
│   ├── position_sizing.py     # Dollar sizing + bracket-price math
│   ├── sms_notifier.py        # WhatsApp approval flow (Twilio)
│   ├── trade_proposal.py      # Immutable proposal value object
│   ├── defensive_monitor.py   # Intraday VIX + position-swing circuit breakers
│   ├── wash_sale_tracker.py   # 30-day wash-sale log + gate
│   ├── reporting.py           # Daily report generation + trade journal
│   └── tests/                 # pytest suite (no live connections required)
├── run.bat                    # Manual launch (Windows)
├── run_scheduled.bat          # Automated launch — used by Task Scheduler
├── requirements.txt
├── .env.example               # Template — copy to .env and fill in credentials
└── logs/                      # Daily output files (gitignored)
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A Robinhood account with agentic trading enabled
- A Twilio account (free trial works) for WhatsApp notifications
- Claude Code CLI (for the Robinhood MCP connection)

```bash
pip install -r requirements.txt
```

### 2. Credentials

```bash
cp .env.example .env
```

Open `.env` and fill in:

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `ROBINHOOD_MCP_TOKEN` | Auto-read from `~/.claude/.credentials.json` after connecting via Claude Code |
| `TWILIO_ACCOUNT_SID` | console.twilio.com |
| `TWILIO_AUTH_TOKEN` | console.twilio.com |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` (Twilio sandbox number) |
| `YOUR_PHONE_NUMBER` | Your number in E.164 format e.g. `+12125551234` |

### 3. Connect Robinhood MCP (one-time)

Open Claude Code, run `/mcp`, find **robinhood-trading**, and connect. Claude Code keeps the token refreshed automatically — the agent reads it from `~/.claude/.credentials.json`.

### 4. Set up WhatsApp notifications (one-time)

1. Open WhatsApp and message **+1 (415) 523-8886**
2. Send the join code shown in your Twilio console under Messaging → Try it out → Send a WhatsApp message
3. You're now enrolled in the sandbox — the agent can message you

### 5. Schedule automatic daily runs (Windows)

Run once to register the 9:45 AM weekday task:

```bash
schtasks /create /tn "RobinhoodTradingAgent" /tr "\"%CD%\run_scheduled.bat\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 09:45 /f
```

Your computer must be **on and not sleeping** at 9:45 AM for the task to fire.

---

## Running manually

```bash
run.bat
```

Or directly:

```bash
python -m trading_agent.main_agent
```

---

## Dry-run mode

`DRY_RUN = True` in `config.py` (default). The full pipeline runs — research, WhatsApp notification, approval gate — but **no orders are sent to Robinhood**. Bracket order details are logged only.

Run several dry-run cycles before going live. When satisfied:

1. Open `trading_agent/config.py`
2. Set `DRY_RUN = False`

> **Note on bracket orders:** Robinhood MCP does not support native OCO (one-cancels-other) orders. The agent places three separate GTC orders (limit buy, stop-loss sell, profit-target sell). When one of the exit orders fills, the other must be cancelled. A mid-day monitoring task to handle this automatically is planned before the live flag is recommended.

---

## Dynamic watchlist

The agent maintains its own watchlist state in `trading_agent/data/watchlist_state.json`. Every Friday, it calls Claude Sonnet to review the past week's screening results and decide which symbols to keep, remove, or add. You never edit the watchlist manually — performance data drives the decisions.

---

## Safety constraints (enforced in code)

- No order placed without your WhatsApp approval
- Hard stop-loss at broker level immediately at entry — never relies on monitoring alone
- Max 2 concurrent positions, max 1 leveraged ETF
- Drawdown breaker at −15% from peak automatically halts new entries
- Earnings exclusion within 7-day window
- Minimum $50M daily dollar volume floor
- Bracket integrity check each monitoring cycle — missing stop triggers emergency alert

---

## License

MIT — see [LICENSE](LICENSE).
