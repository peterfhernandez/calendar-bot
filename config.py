import os as _os

# Load .env file if present (never commit .env to git)
def _load_env(path: str = ".env") -> None:
    try:
        with open(path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    _val = _v.strip().strip('"').strip("'")
                    _os.environ[_k.strip()] = _val
    except FileNotFoundError:
        pass

_load_env()

# Assets to trade
ASSETS = ["BTC", "ETH", "SOL"]
#ASSETS = ["ETH", "SOL"]

# Calendar horizons (days to expiry)
NEAR_DAYS_OPTIONS = [1, 7, 14]
FAR_DAYS_OPTIONS  = [7, 14, 30, 45, 60]

# 1-day near legs are only valid with short far legs — a 1d/30d+ spread is
# unusual and almost always illiquid.  Set to 0 to disable the restriction.
MAX_FAR_DAYS_FOR_1D_NEAR = 14

# Entry filters
MIN_IV_CONTANGO = 0.02   # front IV must exceed back IV by at least 2%
MIN_POP         = 0.45   # minimum probability of profit
MIN_OI_NEAR     = 100    # minimum open interest on near-leg strike
MIN_OI_FAR      = 100    # minimum open interest on far-leg strike
MIN_EV          = 0.05   # minimum expected value as a fraction of net_debit.
                         # 0.0 = reject non-positive EV; 0.10 = EV must be ≥ 10% of debit paid.
                         # e.g. a candidate with net_debit=0.02 BTC and ev_score=0.25
                         # has an expected profit of 25% of the debit (0.005 BTC per contract).

# Liquidity gate (applied just before order submission)
MIN_LEG_BID_SIZE       = 1      # minimum bid-size (contracts) per leg — requires bid_size in TickerSnapshot
MIN_LEG_ASK_SIZE       = 1      # minimum ask-size (contracts) per leg — requires ask_size in TickerSnapshot
MAX_LEG_SPREAD_PCT     = 0.05   # reject if (ask-bid)/mid > 5% on either leg
MAX_ENTRY_PREMIUM      = 0.10   # reject if net_debit > spread_mid * (1 + 10%)
COMBO_FILL_TIMEOUT_SEC = 30     # seconds to wait for combo fill before individual-leg fallback

# Position sizing
MAX_LOSS_PCT       = 0.02  # max 2% of portfolio per trade
MAX_POSITIONS      = 5     # max concurrent open calendar spreads
MAX_TOTAL_RISK_PCT = 0.1   # hard 10% total capital-at-risk across all open positions
MAX_QTY            = 100.0  # hard cap on contracts per trade — guards against near-zero debit producing absurd sizes
MIN_NET_DEBIT      = 0.10   # USD — reject candidates whose debit is so small it cannot be sized sensibly

# Risk-free rate used in Black-Scholes pricing.
# Deribit crypto options have no financing cost baked in, so 0.0 is the
# standard and correct value.  Override only if pricing against collateral
# that earns a yield (e.g. stablecoin margin earning interest).
RISK_FREE_RATE = 0.0   # decimal (0.0 = 0%)

# Stop / take-profit
STOP_PCT        = 0.50  # close if spread value < 50% of debit paid
TAKE_PROFIT_PCT = 1.50  # close if spread value > 150% of debit paid

# Scheduler
SCAN_INTERVAL_SEC    = 300  # 5 minutes
MONITOR_INTERVAL_SEC = 60   # 1 minute

# Trading mode:
#   "paper" → test.deribit.com data, dry-run execution (no orders sent)
#   "test"  → test.deribit.com data, orders placed on test.deribit.com
#   "live"  → www.deribit.com data, orders placed on www.deribit.com (real money)
TRADING_MODE = _os.environ.get("TRADING_MODE", "paper")

# Derived URLs — do not hard-code these in other modules
_LIVE = TRADING_MODE == "live"
DERIBIT_WS_URL   = "wss://www.deribit.com/ws/api/v2"  if _LIVE else "wss://test.deribit.com/ws/api/v2"
DERIBIT_REST_URL = "https://www.deribit.com"           if _LIVE else "https://test.deribit.com"

# Backwards-compatible alias (True for paper or test, False for live)
DERIBIT_PAPER = not _LIVE

# API keys — stored in .env, never committed.
# Paper and test modes share test-exchange credentials.
# Live mode uses production credentials.
DERIBIT_TEST_CLIENT_ID     = _os.environ.get("DERIBIT_TEST_CLIENT_ID",     "")
DERIBIT_TEST_CLIENT_SECRET = _os.environ.get("DERIBIT_TEST_CLIENT_SECRET", "")
DERIBIT_LIVE_CLIENT_ID     = _os.environ.get("DERIBIT_LIVE_CLIENT_ID",     "")
DERIBIT_LIVE_CLIENT_SECRET = _os.environ.get("DERIBIT_LIVE_CLIENT_SECRET", "")

# Active credentials selected by mode
DERIBIT_CLIENT_ID     = DERIBIT_LIVE_CLIENT_ID     if _LIVE else DERIBIT_TEST_CLIENT_ID
DERIBIT_CLIENT_SECRET = DERIBIT_LIVE_CLIENT_SECRET if _LIVE else DERIBIT_TEST_CLIENT_SECRET

DAILY_LOSS_LIMIT = 500    # USD — halt bot if breached; required when TRADING_MODE == "live"

# Alerts
# All alert settings are read from env vars (set in .env, never commit).
# Email — set ALERT_EMAIL to enable; SMTP defaults to Gmail on port 587.
ALERT_EMAIL    = _os.environ.get("ALERT_EMAIL",    "")  # recipient address
SMTP_HOST      = _os.environ.get("SMTP_HOST",      "smtp.gmail.com")
SMTP_PORT      = int(_os.environ.get("SMTP_PORT",  "587"))
SMTP_USER      = _os.environ.get("SMTP_USER",      "")
SMTP_PASSWORD  = _os.environ.get("SMTP_PASS",      "")  # env var is SMTP_PASS; alias here
# Telegram — set both TOKEN and CHAT to enable Telegram alerts.
TELEGRAM_TOKEN      = _os.environ.get("TELEGRAM_TOKEN",      "")  # bot token (TELEGRAM_TOKEN in .env)
TELEGRAM_BOT_TOKEN  = TELEGRAM_TOKEN                               # alias for compatibility
TELEGRAM_CHAT       = _os.environ.get("TELEGRAM_CHAT",       "")  # chat ID  (TELEGRAM_CHAT in .env)
TELEGRAM_CHAT_ID    = TELEGRAM_CHAT                                # alias for compatibility
