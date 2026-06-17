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

A fully-automated Python swing-trading research agent for a small Robinhood account. Every weekday it:

1. Pulls free market data via **yfinance** (no API cost)
2. Runs a 6-step technical screening pipeline on a 17-symbol watchlist
3. Checks macro conditions (SPY trend + VIX level)
4. If a stock passes all screens → sends you a **WhatsApp message** with the trade details
5. You reply **YES** or **NO** from your phone
6. On YES → places a bracket order on Robinhood (limit buy + stop-loss + profit target)
7. Saves a daily log to `logs/YYYYMMDD.txt`

If no setups are found, it sends a brief WhatsApp summary so you know it ran.

---

## Automated schedule (Windows Task Scheduler)

| Time | Task | What it does |
|---|---|---|
| 9:45 AM Mon–Fri | Research scan | First look — screens all symbols after open |
| 10:30 AM Mon–Fri | Research scan | Volume builds after open — catches early momentum |
| 11:30 AM Mon–Fri | Research scan | Mid-morning confirmation — most reliable volume reading |
| 1:00 PM Mon–Fri | Research scan | Post-lunch check — catches afternoon setups |
| 12:30 PM Mon–Fri | Bracket monitor | Cancels orphaned exit orders, verifies all stops are active |
| 3:30 PM Mon–Fri | Bracket monitor | Same check before close |

Your computer must be **on and not sleeping** for scheduled tasks to fire.

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
| Position size | 15–30% of account per trade (risk-based sizing) |
| Max concurrent positions | 2 |
| Cash reserve (always kept back) | 15% |
| Stop loss | 3–8% below entry (based on nearest support) |
| Profit target | 5–15% above entry (based on nearest resistance) |
| Minimum reward:risk required | 1.5:1 |
| Drawdown breaker | −15% from peak → pause all new entries |

With a $250 account: each position is ~$68, worst-case loss if both stop out = ~$8.16 (−3.3% of account).
Targets are set at actual resistance levels — never invented. If no valid resistance exists, the trade is rejected.

---

## Screening pipeline (6 steps)

1. **Universe scan** — active watchlist from WatchlistManager (starts with 14 symbols)
2. **Technical screen** — price above MA50, RSI momentum (RSI > 50 and rising) **AND** volume confirmation (both required), support level within 3–8% for stop placement (50-day lookback)
3. **Catalyst check** — excludes stocks with earnings within 7 days, or avg daily dollar volume < $50M
4. **Risk/reward calc** — computes stop and target from support/resistance; rejects if R:R < 1.5
5. **Macro gate** — SPY vs MA50 + VIX level → NORMAL / RAISE_BAR / NO_TRADE
6. **Rank** — top 2 candidates by reward:risk ratio

### Signal rationale (from backtesting)
A 2-year walk-forward backtest over 500 trading days produced these findings:

- **RSI momentum AND volume** (both required): 71 signals, **43.7% win rate**, +0.68% expectancy, only 1.4% forced exits
- **RSI OR volume** (old logic): 179 signals, same win rate, but 13% forced exits — the AND requirement filters to cleaner setups at real support/resistance
- **RSI bounce** (cross up from below 30 while in uptrend): 0 signals — removed (logically contradictory with uptrend requirement)
- **ADX trend filter** (tested, not used as gate): improves false-positive filtering in theory but causes late-trend entries in practice — stored as informational field for future tuning
- Best performers: SMCI 71%, TSLA 55%, SOXL 50%, NVDA 50%
- Removed from watchlist based on backtest data: COIN (8%), AVGO (0%), AAPL (0%), MSFT (0%), GOOGL (0%) — these don't move enough to reliably hit a 5% target

---

## Bracket order monitoring

Robinhood MCP does not support native OCO (one-cancels-other) orders. The agent places three separate GTC orders (limit buy, stop-loss sell, profit-target sell). When one exit order fills, the other remains open.

The 12:30 PM and 3:30 PM monitor tasks handle this automatically:
- Detects any sell order with no matching position (orphaned after a fill)
- Cancels the orphaned order before it can trigger an unintended short sale
- Sends a WhatsApp alert if an emergency is found

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
├── backtest.py                # Walk-forward backtester using historical yfinance data
├── run_scheduled.bat          # Research mode — used by Task Scheduler (9:45 AM + 1 PM)
├── run_monitor.bat            # Monitor mode — used by Task Scheduler (12:30 PM + 3:30 PM)
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

Run these once to register all four tasks:

```bash
# Morning research scan — 9:45 AM
schtasks /create /tn "RobinhoodAgent-Morning" /tr "C:\full\path\to\run_scheduled.bat" /sc WEEKLY /d MON,TUE,WED,THU,FRI /st 09:45 /f

# 10:30 AM scan
schtasks /create /tn "RobinhoodAgent-1030" /tr "C:\full\path\to\run_scheduled.bat" /sc WEEKLY /d MON,TUE,WED,THU,FRI /st 10:30 /f

# 11:30 AM scan
schtasks /create /tn "RobinhoodAgent-1130" /tr "C:\full\path\to\run_scheduled.bat" /sc WEEKLY /d MON,TUE,WED,THU,FRI /st 11:30 /f

# 1:00 PM scan
schtasks /create /tn "RobinhoodAgent-Midday" /tr "C:\full\path\to\run_scheduled.bat" /sc WEEKLY /d MON,TUE,WED,THU,FRI /st 13:00 /f

# Bracket monitor — 12:30 PM
schtasks /create /tn "RobinhoodAgent-Monitor1230" /tr "C:\full\path\to\run_monitor.bat" /sc WEEKLY /d MON,TUE,WED,THU,FRI /st 12:30 /f

# Bracket monitor — 3:30 PM
schtasks /create /tn "RobinhoodAgent-Monitor1530" /tr "C:\full\path\to\run_monitor.bat" /sc WEEKLY /d MON,TUE,WED,THU,FRI /st 15:30 /f
```

Replace `C:\full\path\to\` with the actual path to your project folder.

---

## Running manually

```bash
# Research mode
python -m trading_agent.main_agent

# Monitor mode
python -m trading_agent.main_agent monitor
```

---

## Dry-run mode

`DRY_RUN = True` in `config.py` (default). The full pipeline runs — research, WhatsApp notification, approval gate — but **no orders are sent to Robinhood**. Bracket order details are logged only.

Run several dry-run cycles before going live. When satisfied:

1. Open `trading_agent/config.py`
2. Set `DRY_RUN = False`

---

## Dynamic watchlist + discovery

The agent maintains its own watchlist state in `trading_agent/data/watchlist_state.json`. You never edit it manually — the pipeline manages it automatically.

**Active watchlist (14 symbols):** NVDA, AMD, TSLA, META, AMZN, SPY, QQQ, SOXL, TQQQ, XLE, XLF, NFLX, PLTR, SMCI

Removed based on backtesting: AAPL, MSFT, GOOGL (0% win rate — too slow-moving), COIN (8%), AVGO (0%)

**Discovery universe (15 symbols):** scanned every morning alongside the active watchlist. Any symbol that passes the technical screen is automatically flagged as a candidate.

| Type | Symbols |
|---|---|
| Sector / thematic ETFs | SPXL, IWM, ARKK, GLD, XLK |
| High-beta large-caps | MSTR, SHOP, SNOW, HOOD, RIVN, CRWD, DKNG, ROKU, RBLX, UBER |

**Friday review:** Claude Sonnet looks at the week's performance data for all active symbols plus any new candidates and decides what to keep, remove, or formally add. The decision is logged and a WhatsApp summary is sent.

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

## Backtesting

Run `backtest.py` to evaluate signal quality over 2 years of history:

```bash
python backtest.py
```

Uses the exact same screening logic as the live agent — no code duplication, no look-ahead bias. Outputs win rate, expectancy, and per-symbol breakdown so you can evaluate changes before deploying them.

---

## License

MIT — see [LICENSE](LICENSE).
