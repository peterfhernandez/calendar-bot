import os as _os
from pathlib import Path as _Path

# Load .env file if present (never commit .env to git).
# Uses setdefault so that values already in os.environ (e.g. pre-loaded by
# bot.py's --env pre-parser) are not overwritten.
def _load_env(path: str | None = None) -> None:
    if path is None:
        # Honour BOT_ENV_FILE if set by the --env pre-parser in bot.py.
        path = _os.environ.get("BOT_ENV_FILE", ".env")
    try:
        with open(path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    _val = _v.strip().strip('"').strip("'")
                    _os.environ.setdefault(_k.strip(), _val)
    except FileNotFoundError:
        pass

_load_env()

# Assets the bot will trade (scanner, decision engine, execution)
ASSETS = ["BTC","ETH"]

# Assets the data collector will gather option-chain snapshots for.
# Can be a superset of ASSETS — useful for collecting data on assets
# (e.g. SOL) that you want to analyse or backtest without trading them yet.
COLLECTOR_ASSETS = ["BTC", "ETH", "SOL"]

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
MIN_EV          = 0.25   # minimum expected value as a fraction of net_debit.
                         # 0.0 = reject non-positive EV; 0.10 = EV must be ≥ 10% of debit paid.
                         # e.g. a candidate with net_debit=0.02 BTC and ev_score=0.25
                         # has an expected profit of 25% of the debit (0.005 BTC per contract).

# Per-asset threshold overrides.
# Any key present here takes precedence over the corresponding global default
# for that specific asset.  SOL options are significantly thinner than BTC/ETH:
# lower open interest, wider bid/ask spreads, and a less stable IV term
# structure.  These overrides let SOL participate without loosening the global
# filters that protect BTC/ETH entries.
ASSET_OVERRIDES: dict = {
    "SOL": {
        "MIN_OI_NEAR":        10,    # global: 100
        "MIN_OI_FAR":         10,    # global: 100
        "MAX_LEG_SPREAD_PCT": 0.20,  # global: 0.05
        "MAX_ENTRY_PREMIUM":  0.20,  # global: 0.10
        "MIN_IV_CONTANGO":    0.01,  # global: 0.02
    }
}


def asset_config(asset: str, key: str):
    """Return the per-asset override for *key*, or the module-level global default."""
    override = ASSET_OVERRIDES.get(asset.upper(), {}).get(key)
    return override if override is not None else globals()[key]


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
CHAIN_CACHE_TTL_SEC  = 30   # seconds before a cached ticker snapshot is considered stale

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

# Fee model (Deribit options schedule)
# Verify against: support.deribit.com/hc/en-us/articles/25944746248989
OPTIONS_FEE_PCT           = 0.0003   # 0.03% of underlying per leg per trade (BTC and ETH taker+maker; SOL taker)
OPTIONS_MIN_FEE_BTC       = 0.0003   # minimum fee in BTC per contract
OPTIONS_MIN_FEE_ETH       = 0.0003   # minimum fee in ETH per contract
OPTIONS_MIN_FEE_SOL       = 0.0003   # minimum fee in SOL per contract (taker only; maker = 0%)
SOL_MAKER_FEE_PCT         = 0.0      # SOL options maker fee is zero
OPTIONS_DELIVERY_FEE_PCT  = 0.00015  # 0.015% of underlying at expiry for monthly+ options
OPTIONS_DELIVERY_FEE_CAP  = 0.125    # delivery fee capped at 12.5% of option market value
COMBO_CHEAP_LEG_DISCOUNT  = 1.0      # 100% taker discount on the cheaper leg of a combo order
# No delivery fee for daily (1d) or weekly (7d) near legs — only monthly and longer

# Drain mode — set to True to stop entering new trades and disable near-leg
# rolling.  Existing positions are monitored normally; stop-loss and
# take-profit triggers fire as usual.  Near legs approaching expiry are closed
# outright rather than rolled.  Use this to wind down all open positions
# without starting new ones.
DRAIN_MODE = _os.environ.get("DRAIN_MODE", "").lower() in ("1", "true", "yes")

# Drain-and-new mode — like DRAIN_MODE for existing positions (no rolling,
# close outright) but new entries ARE allowed.  Set at runtime via
# /drain_and_new Telegram command.  Takes precedence over DRAIN_MODE when True.
DRAIN_AND_NEW_MODE: bool = False

# Portfolio override — when set to a positive float, replaces the live
# available_cash reported by PortfolioTracker for all sizing decisions.
# Set at runtime via /drain_and_new portfolio=N or /start_with_assets.
# Set back to None to resume using the live tracker value.
PORTFOLIO_OVERRIDE: float | None = None

# Cross Portfolio Margin (X:PM) entry gate — Phase 17
# Before entering a new position, check Deribit's actual margin requirement
# and reject the trade if it would push the account past this utilization ceiling.
# The gate's primary path asks Deribit for a live margin simulation; if that
# fails, it falls back to a conservative local proxy (maintenance_margin / equity).
MAX_MARGIN_UTILIZATION_PCT = 0.80  # ceiling on maintenance_margin / equity (Deribit's default is same)
MARGIN_GATE_ENABLED = True  # kill switch — set False to disable the gate entirely
MARGIN_GATE_REQUIRED_LIVE = True  # in test/live mode, missing/failed margin data blocks entry (fail closed)
# In paper mode, the gate is a no-op by default so paper trading is not blocked
# by the absence of a funded test account; set MARGIN_GATE_ENABLED=True in
# config_test.py to force it on for testing.

# Alerts
# All alert settings are read from env vars (set in .env, never commit).
# Email — set ALERT_EMAIL to enable; SMTP defaults to Gmail on port 587.
ALERT_EMAIL    = _os.environ.get("ALERT_EMAIL",    "")  # recipient address
SMTP_HOST      = _os.environ.get("SMTP_HOST",      "smtp.gmail.com")
SMTP_PORT      = int(_os.environ.get("SMTP_PORT",  "587"))
SMTP_USER      = _os.environ.get("SMTP_USER",      "")
SMTP_PASSWORD  = _os.environ.get("SMTP_PASS",      "")  # env var is SMTP_PASS; alias here
SMTP_FROM      = _os.environ.get("SMTP_FROM",      "") or SMTP_USER  # sender address (defaults to SMTP_USER)
# Telegram — set both TOKEN and CHAT to enable Telegram alerts.
TELEGRAM_TOKEN      = _os.environ.get("TELEGRAM_TOKEN",      "")  # bot token (TELEGRAM_TOKEN in .env)
TELEGRAM_BOT_TOKEN  = TELEGRAM_TOKEN                               # alias for compatibility
TELEGRAM_CHAT       = _os.environ.get("TELEGRAM_CHAT",       "")  # chat ID  (TELEGRAM_CHAT in .env)
TELEGRAM_CHAT_ID    = TELEGRAM_CHAT                                # alias for compatibility

# ── Logging (Phase 20a) ───────────────────────────────────────────────────────
# Shared by core/logging_setup.py::setup_logging() — every entry point (bot,
# collector, debug viewer, standalone feed) uses the same format and rotation.
LOG_LEVEL           = "INFO"                                      # root logger level
LOG_FORMAT          = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
LOG_DATE_FORMAT     = "%Y-%m-%d %H:%M:%S"
LOG_FILE_MAX_BYTES  = 10 * 1024 * 1024   # rotate the log file at 10 MB
LOG_BACKUP_COUNT    = 5                   # keep this many rotated files
LOG_DIR             = "logs"              # default directory for bot.log

# Third-party loggers that flood the log at INFO (httpx logs every Telegram
# getUpdates poll).  Each entry is logger name → minimum level to allow.
NOISY_LOGGERS = {
    "httpx":                       "WARNING",
    "httpcore":                    "WARNING",
    "telegram.ext.Updater":        "WARNING",
    "telegram.vendor.ptb_urllib3": "WARNING",
}

# Per-module level overrides applied by bot.py at startup (previously a silent
# hardcoded exception in bot.py).  Logger name → level.
LOG_LEVEL_OVERRIDES = {
    "strategy.decision": "DEBUG",
    "strategy.sizer":    "DEBUG",
}

# ── Network / timeout / retry constants (Phase 20b + 20c) ─────────────────────
# Deribit WebSocket connection parameters — shared by data/deribit_feed.py,
# execution/executor.py, and execution/order_manager.py.
DERIBIT_WS_PING_INTERVAL = 20                 # seconds between WS keepalive pings
DERIBIT_WS_PING_TIMEOUT  = 20                 # seconds to wait for a pong before dropping
DERIBIT_WS_OPEN_TIMEOUT  = 15                 # seconds to wait for the WS handshake
DERIBIT_WS_MAX_SIZE      = 10 * 1024 * 1024   # 10 MB — large option-chain snapshots
RPC_TIMEOUT_SEC          = 15                 # seconds to wait for a JSON-RPC response

# Order execution (execution/executor.py)
SLIPPAGE_LIMIT_PCT = 0.02        # reject fills deviating more than 2% from intended price
ORDER_TIMEOUT_SEC  = 30          # seconds to wait for an order to fill before giving up
MAX_ORDER_RETRIES  = 3           # submit attempts per leg on transient network errors
ORDER_RETRY_DELAYS = [1, 3, 9]   # seconds between those attempts (len >= MAX_ORDER_RETRIES - 1)

# Order lifecycle tracking (execution/order_manager.py)
STUCK_ORDER_TIMEOUT_SEC = 120    # open orders older than this are flagged as stuck

# Alerts (alerts/notifier.py)
ALERT_COOLDOWN_SEC   = 300   # suppress duplicate alerts with the same key inside this window
SMTP_TIMEOUT_SEC     = 10    # SMTP connection timeout
TELEGRAM_TIMEOUT_SEC = 10    # Telegram Bot API request timeout

# Data collector (backtest/data_collector.py, collect.py)
COLLECTOR_INTERVAL_SEC = 300   # seconds between option-chain snapshots

# ── Business-logic thresholds (Phase 20e) ─────────────────────────────────────
# Strike increment lookup: (max_spot_exclusive, increment) rows, first match wins;
# spots above the last row use STRIKE_INCREMENT_DEFAULT.  (core/pricing.py)
STRIKE_INCREMENT_TABLE = [
    (5,     0.50),
    (20,    1.0),
    (100,   5.0),
    (500,   10.0),
    (2_000, 50.0),
]
STRIKE_INCREMENT_DEFAULT = 100.0

# Far-leg bid/ask spread model: (max_days_to_expiry, spread_pct of mid) rows,
# first match wins; longer-dated legs use FAR_LEG_SPREAD_DEFAULT.  Beyond 30
# days an extra liquidity penalty accrues per 30 days.  (core/pricing.py)
FAR_LEG_SPREAD_TABLE = [
    (7,  0.005),
    (14, 0.010),
    (30, 0.015),
]
FAR_LEG_SPREAD_DEFAULT           = 0.025
FAR_LEG_LIQUIDITY_PENALTY_PER_30D = 0.005   # added per 30 days beyond 30d to expiry

# Scanner DTE matching (strategy/scanner.py)
NEAR_DAY_TOLERANCE = 3    # accept near legs within ±N days of each target
FAR_DAY_TOLERANCE  = 7    # accept far legs within ±N days of each target
EV_SAMPLE_COUNT    = 40   # spot-grid samples for the EV score integration

# Breakeven scan (core/calendar_engine.py) — resolution and spot range of the
# numeric breakeven search; the range is also the scanner's full-profit fallback.
BREAKEVEN_SCAN_STEPS = 800
BREAKEVEN_SCAN_RANGE = (0.50, 1.50)   # scan spot × [lo … hi]

# Spread-status warning threshold: warn (no action) when the spread value falls
# to this fraction of the debit paid; STOP_PCT remains the hard stop.
SPREAD_WARN_PCT = 0.70

# Decision engine (strategy/decision.py)
ROLL_TRIGGER_DAYS          = 2   # days before near-leg expiry at which rolling is considered
POSITION_FAILURE_RETRY_CAP = 3   # failed close/roll attempts before a position is marked stuck

# Position sizing (strategy/sizer.py, execution/executor.py)
MIN_CONTRACT_SIZE      = 0.1    # Deribit minimum option contract increment (BTC/ETH)
STRIKE_CORRELATION_PCT = 0.05   # positions within ±5% of an open strike are correlated

# Portfolio tracker (portfolio/tracker.py)
RECONCILE_THRESHOLD_PCT = 0.10      # warn when Deribit vs DB margin diverge by more than 10%
INITIAL_CAPITAL         = 10_000.0  # paper-mode starting equity for the DB-only portfolio

# Default portfolio value used for sizing when no live tracker value or CLI
# override is provided (bot.py --portfolio, executor, backtest engine).
DEFAULT_PORTFOLIO_VALUE = 10_000.0

# ── Paths / timezone / date format (Phase 20f) ────────────────────────────────
# SQLite trade database.  BOT_DB_PATH (set by bot.py's --db pre-parser or the
# instance's .env) overrides the default so parallel instances stay isolated.
DB_PATH = _Path(_os.environ.get("BOT_DB_PATH", str(_Path(__file__).parent / "db" / "calendar_bot.db")))

# DuckDB historical option-chain database written by the data collector.
HISTORIC_DATA_DB_PATH = _Path(__file__).parent / "backtest" / "historic_data" / "options.duckdb"

# Timezone for "today" boundaries in Telegram day queries (db/state.py).
TIMEZONE = "Australia/Sydney"

# Date format for DB date columns and chart axis labels (telegram_cmd/pnl_chart.py).
DATE_FORMAT = "%Y-%m-%d"

# Config override — exec a per-instance Python file (BOT_CONFIG_FILE env var,
# set via --config CLI flag) so strategy parameters can differ between a
# paper-mode and a test-mode instance without forking the whole config.
# The override file is plain Python; assign only the variables you want to
# change.  Example (config_test.py):
#   ASSETS       = ["BTC"]
#   MAX_POSITIONS = 1
#   MAX_LOSS_PCT  = 0.005
_cfg_override = _os.environ.get("BOT_CONFIG_FILE", "")
if _cfg_override:
    try:
        with open(_cfg_override) as _f:
            exec(compile(_f.read(), _cfg_override, "exec"), globals())
    except FileNotFoundError:
        raise SystemExit(f"Config override file not found: {_cfg_override!r}")
