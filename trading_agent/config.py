"""
Central configuration for the trading agent.
All constants are defined here — adjust values before running.
"""

# ---------------------------------------------------------------------------
# Models — Sonnet or cheaper only. Opus is never used.
# ---------------------------------------------------------------------------
MODEL_DECISIONS = "claude-sonnet-4-6"          # watchlist review, complex reasoning
MODEL_DATA      = "claude-haiku-4-5-20251001"  # MCP tool calls, data fetching, formatting

# ---------------------------------------------------------------------------
# Account sizing
# ---------------------------------------------------------------------------
STARTING_CAPITAL       = 250.00
RISK_PER_TRADE_PCT     = 0.015  # risk 1.5% of account per trade (~$3.75 on $250)
POSITION_SIZE_PCT_MIN  = 0.15   # never less than 15% of account (too small to matter)
POSITION_SIZE_PCT_MAX  = 0.30   # never more than 30% of account regardless of stop
CASH_RESERVE_PCT_MIN   = 0.15   # must keep at least 15% in cash

# ---------------------------------------------------------------------------
# Concurrency limits
# ---------------------------------------------------------------------------
MAX_CONCURRENT_POSITIONS    = 2
MAX_LEVERAGED_ETF_POSITIONS = 1

# ---------------------------------------------------------------------------
# Exit bracket bounds
# ---------------------------------------------------------------------------
STOP_LOSS_PCT_MIN    = 0.03   # 3%  — below this gets hit by normal daily noise
STOP_LOSS_PCT_MAX    = 0.08   # 8%  — above this risks too much per trade
PROFIT_TARGET_PCT_MIN = 0.05  # 5%  — below this isn't worth the trade
PROFIT_TARGET_PCT_MAX = 0.15  # 15% — above this is unrealistic for a 4-week swing

# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------
ACCOUNT_DRAWDOWN_BREAKER_PCT = 0.15  # -15% from peak -> pause new trades

# ---------------------------------------------------------------------------
# Liquidity / event filters
# ---------------------------------------------------------------------------
MIN_AVG_DAILY_DOLLAR_VOLUME  = 50_000_000  # $50M daily dollar volume floor
EARNINGS_EXCLUSION_WINDOW_DAYS = 7

# ---------------------------------------------------------------------------
# Volatility / intraday thresholds
# ---------------------------------------------------------------------------
VIX_HIGH_THRESHOLD         = 25
VIX_INTRADAY_SPIKE_PCT     = 0.20   # 20% intraday VIX spike
POSITION_INTRADAY_SWING_PCT = 0.10  # 10% intraday move on a held position

# ---------------------------------------------------------------------------
# Proposal lifecycle
# ---------------------------------------------------------------------------
ENTRY_RECOMMENDATION_EXPIRY_HOURS = 2

# ---------------------------------------------------------------------------
# Wash-sale window
# ---------------------------------------------------------------------------
WASH_SALE_WINDOW_DAYS = 30

# ---------------------------------------------------------------------------
# Watchlist — seed only. After first run, WatchlistManager owns the list.
# These are the starting symbols; the research agent adds/removes from here.
# ---------------------------------------------------------------------------
WATCHLIST_SEED = [
    "NVDA", "AMD",  "TSLA", "META", "AMZN",
    "SPY",  "QQQ",  "SOXL", "TQQQ", "XLE",  "XLF",  "NFLX",
    "PLTR", "SMCI",
    # Removed: COIN  (8% win rate over 13 backtest signals)
    # Removed: AVGO  (0% win rate over 3 backtest signals)
    # Removed: AAPL, MSFT, GOOGL (0% win rate — don't move enough to hit 5% target)
]

# Always included regardless of watchlist state — needed for macro checks
WATCHLIST_PERMANENT = ["SPY", "QQQ"]

LEVERAGED_ETFS = ["SOXL", "TQQQ", "SPXL"]  # subset flagged as leveraged/3x

# ---------------------------------------------------------------------------
# Discovery universe — scanned daily for new candidates.
# These are NOT on the active watchlist but will be registered as candidates
# if they pass the technical screen. The Friday review then decides whether
# to formally add them. Kept to high-beta, liquid, well-known names only.
# ---------------------------------------------------------------------------
DISCOVERY_UNIVERSE = [
    # Sector / thematic ETFs
    "SPXL", "IWM",  "ARKK", "GLD",  "XLK",
    # High-beta large-caps matching winner profile
    "MSTR", "SHOP", "SNOW", "HOOD", "RIVN",
    "CRWD", "DKNG", "ROKU", "RBLX", "UBER",
]

# How many consecutive research days with no setup before a symbol is flagged
WATCHLIST_FAIL_THRESHOLD = 15       # ~3 trading weeks
# Max symbols in active watchlist (keeps morning run fast)
WATCHLIST_MAX_ACTIVE = 60

# ---------------------------------------------------------------------------
# Technical indicator periods
# ---------------------------------------------------------------------------
RSI_PERIOD     = 14
MA_SHORT_PERIOD = 20
MA_LONG_PERIOD  = 50

# ---------------------------------------------------------------------------
# Data / persistence paths
# ---------------------------------------------------------------------------
TRADE_JOURNAL_PATH  = "trading_agent/data/trade_journal.jsonl"
WATCHLIST_STATE_PATH = "trading_agent/data/watchlist_state.json"

# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------
DRY_RUN = True   # set False only after dry-run validation
