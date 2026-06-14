# Assets to trade
ASSETS = ["BTC", "ETH"]

# Calendar horizons (days to expiry)
NEAR_DAYS_OPTIONS = [7, 14]
FAR_DAYS_OPTIONS  = [30, 45, 60]

# Entry filters
MIN_IV_CONTANGO = 0.02   # front IV must exceed back IV by at least 2%
MIN_POP         = 0.45   # minimum probability of profit
MIN_OI_NEAR     = 100    # minimum open interest on near-leg strike
MIN_OI_FAR      = 100    # minimum open interest on far-leg strike

# Position sizing
MAX_LOSS_PCT  = 0.02  # max 2% of portfolio per trade
MAX_POSITIONS = 3     # max concurrent open calendar spreads

# Stop / take-profit
STOP_PCT        = 0.50  # close if spread value < 50% of debit paid
TAKE_PROFIT_PCT = 1.50  # close if spread value > 150% of debit paid

# Scheduler
SCAN_INTERVAL_SEC    = 300  # 5 minutes
MONITOR_INTERVAL_SEC = 60   # 1 minute

# Broker
DERIBIT_PAPER    = True   # set False for live trading
DAILY_LOSS_LIMIT = 500    # USD — halt bot if breached

# Alerts
ALERT_EMAIL    = ""  # SMTP recipient; leave empty to disable
TELEGRAM_TOKEN = ""  # bot token; leave empty to disable
TELEGRAM_CHAT  = ""  # chat ID; leave empty to disable
