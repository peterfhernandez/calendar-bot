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
#ASSETS = ["BTC", "ETH", "SOL"]
ASSETS = ["ETH", "BTC"]

# Calendar horizons (days to expiry)
NEAR_DAYS_OPTIONS = [7, 14]
FAR_DAYS_OPTIONS  = [30, 45, 60]

# Entry filters
MIN_IV_CONTANGO = 0.02   # front IV must exceed back IV by at least 2%
MIN_POP         = 0.45   # minimum probability of profit
MIN_OI_NEAR     = 100    # minimum open interest on near-leg strike
MIN_OI_FAR      = 100    # minimum open interest on far-leg strike
MIN_EV          = 0.05   # minimum expected value as a fraction of net_debit.
                         # 0.0 = reject non-positive EV; 0.10 = EV must be ≥ 10% of debit paid.
                         # e.g. a candidate with net_debit=0.02 BTC and ev_score=0.25
                         # has an expected profit of 25% of the debit (0.005 BTC per contract).

# Position sizing
MAX_LOSS_PCT       = 0.02  # max 2% of portfolio per trade
MAX_POSITIONS      = 5     # max concurrent open calendar spreads
MAX_TOTAL_RISK_PCT = 0.09  # hard 6% total capital-at-risk across all open positions

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

# Broker
DERIBIT_PAPER         = True   # set False for live trading
DAILY_LOSS_LIMIT      = 500    # USD — halt bot if breached
DERIBIT_CLIENT_ID     = _os.environ.get("DERIBIT_CLIENT_ID",     "")
DERIBIT_CLIENT_SECRET = _os.environ.get("DERIBIT_CLIENT_SECRET", "")

# Alerts
# SMTP credentials are read from env vars: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
# (defaults: smtp.gmail.com:587).  Set ALERT_EMAIL to enable email alerts.
ALERT_EMAIL    = _os.environ.get("ALERT_EMAIL",    "")  # SMTP recipient
TELEGRAM_TOKEN = _os.environ.get("TELEGRAM_TOKEN", "")  # bot token
TELEGRAM_CHAT  = _os.environ.get("TELEGRAM_CHAT",  "")  # chat ID
