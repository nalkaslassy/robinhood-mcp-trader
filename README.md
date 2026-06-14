# Robinhood MCP Trading Agent

> **DISCLAIMER — PLEASE READ BEFORE USE**
>
> This project is for **educational and portfolio demonstration purposes only**.
> It is **not financial advice**. Trading involves substantial risk of loss.
> Past performance of any strategy does not guarantee future results.
> **You assume all risk** for any live trading you conduct using this software.
> The author(s) are not registered investment advisers and make no representation
> about the suitability of this strategy for any particular investor.
> Never risk money you cannot afford to lose.

---

## Overview

A Python-based swing-trading research agent for a small Robinhood account.

**Design principles:**
- **Human-in-the-loop for entries**: every new position requires explicit approval before an order is sent.
- **Automated exits**: bracket orders (stop-loss + profit-target) are placed immediately at entry as hard broker-side orders.
- **Rules-based research**: a 6-step pipeline screens candidates from a fixed watchlist, checks macro conditions (SPY trend + VIX), and ranks by reward:risk ratio.
- **Multiple circuit breakers**: account drawdown breaker, intraday VIX spike gate, earnings-proximity exclusion, liquidity floor.

The agent was designed with a $250 starting account; all percentages scale to any account size.

---

## Project structure

```
trading_agent/
├── config.py              # All constants — adjust before running
├── account_state.py       # Account value, drawdown, position limits
├── research_engine.py     # 6-step daily research pipeline
├── position_sizing.py     # Dollar sizing + bracket-price math
├── wash_sale_tracker.py   # 30-day wash-sale log + gate
├── trade_proposal.py      # Immutable proposal value object
├── defensive_monitor.py   # Intraday VIX + position-swing circuit breakers
├── reporting.py           # Daily/weekly/monthly reports + trade journal
├── order_executor.py      # Robinhood MCP integration (swap-ready)
├── main_agent.py          # Orchestration + CLI entry point
└── tests/                 # pytest suite (no live MCP required)
```

---

## Prerequisites

- Python 3.11+
- `pytest` for tests
- A running [Robinhood MCP server](https://github.com/YOUR_MCP_REPO) for live use

```bash
pip install pytest
```

---

## Setup

1. **Clone the repo**
   ```bash
   git clone https://github.com/YOUR_USERNAME/robinhood-mcp-trader.git
   cd robinhood-mcp-trader
   ```

2. **Configure credentials**
   ```bash
   cp .env.example .env
   # Edit .env and fill in your Robinhood MCP credentials
   ```

3. **Review config**  
   Open `trading_agent/config.py` and verify all constants match your risk tolerance before connecting to a live account.

4. **Run the test suite** (no MCP required)
   ```bash
   pytest trading_agent/tests/ -v
   ```
   All tests must pass before proceeding to live use.

---

## Running in dry-run mode (recommended first step)

`config.py` has `DRY_RUN = True` by default. In this mode the agent runs the
full research pipeline and generates proposals, but **no orders are sent** to
Robinhood. Bracket order details are logged to stdout only.

```bash
python -m trading_agent.main_agent
```

Run at least one full dry-run cycle and verify the proposals make sense before
enabling live orders.

---

## Enabling live order placement

Only after successful dry-run validation:

1. Confirm your Robinhood MCP server supports the required order types (see
   `order_executor.py` — note the OCO limitation comment).
2. Set `DRY_RUN = False` in `config.py`.
3. Human approval is still required for every new entry — the agent will prompt
   via stdin (or your configured approval callback).

---

## Key safety constraints

These are non-negotiable and enforced in code:

- **No new order without human approval** — `place_bracket_order()` checks
  proposal expiry and current price range before sending anything.
- **Hard stop-loss at broker level** — stop orders are placed immediately at
  entry; the agent never relies solely on monitoring for downside protection.
- **Max 2 concurrent positions, max 1 leveraged ETF** — enforced in
  `can_open_new_position()`.
- **Drawdown breaker at −15% from peak** — automatically pauses all new entries.
- **Hard earnings exclusion** — any symbol with earnings within 7 days is
  excluded regardless of setup quality.
- **Bracket integrity checks** — every monitoring cycle verifies stop + target
  orders are live; missing stop triggers an EMERGENCY log.

---

## Phased design

**Stage 1 (this repo):** Human approves every entry. Automated bracket exits.
$250 starting capital. Max 2 positions.

**Stage 2 (future):** After ~3 months of Stage 1 data showing positive
expectancy, consider auto-approval for highest-conviction setups with tighter
rules, and position sizing based on Kelly/fixed-fraction.

---

## Contributing

Contributions are welcome. Please open an issue before submitting large
changes. Focus areas: improving the research pipeline's signal quality,
better support for real Robinhood MCP OCO orders, and alternative data sources
for market data.

Bug reports and tests for edge cases are especially appreciated.

---

## License

MIT — see [LICENSE](LICENSE).
