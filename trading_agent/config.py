"""
Central configuration for the trading agent.
All constants are defined here — adjust values before running.
"""

# Account sizing
STARTING_CAPITAL = 250.00
POSITION_SIZE_PCT_MIN = 0.25   # 25% of current account value
POSITION_SIZE_PCT_MAX = 0.30   # 30% of current account value
CASH_RESERVE_PCT_MIN = 0.15    # must keep at least 15% in cash

# Concurrency limits
MAX_CONCURRENT_POSITIONS = 2
MAX_LEVERAGED_ETF_POSITIONS = 1  # of the max positions, at most 1 may be leveraged/3x

# Exit bracket bounds
STOP_LOSS_PCT_MIN = 0.05   # 5%
STOP_LOSS_PCT_MAX = 0.07   # 7%
PROFIT_TARGET_PCT_MIN = 0.02  # 2%
PROFIT_TARGET_PCT_MAX = 0.04  # 4%

# Circuit breakers
ACCOUNT_DRAWDOWN_BREAKER_PCT = 0.15  # -15% from peak -> pause new trades

# Liquidity / event filters
MIN_AVG_DAILY_DOLLAR_VOLUME = 50_000_000   # $50M
EARNINGS_EXCLUSION_WINDOW_DAYS = 7

# Volatility / intraday thresholds
VIX_HIGH_THRESHOLD = 25
VIX_INTRADAY_SPIKE_PCT = 0.20       # 20% intraday VIX spike
POSITION_INTRADAY_SWING_PCT = 0.10  # 10% intraday move on a held position

# Proposal lifecycle
ENTRY_RECOMMENDATION_EXPIRY_HOURS = 2

# Wash-sale window
WASH_SALE_WINDOW_DAYS = 30

# Universe
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "GOOGL", "AMZN",
    "SPY", "QQQ", "SOXL", "TQQQ", "XLE", "XLF", "AVGO", "NFLX",
    "COIN", "PLTR", "SMCI",
]
LEVERAGED_ETFS = ["SOXL", "TQQQ"]  # subset flagged as leveraged/3x

# Technical indicator periods
RSI_PERIOD = 14
MA_SHORT_PERIOD = 20
MA_LONG_PERIOD = 50

# Data / persistence
TRADE_JOURNAL_PATH = "trading_agent/data/trade_journal.jsonl"

# Run modes
DRY_RUN = True   # set False only after dry-run validation
