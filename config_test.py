import os as _os

# Load .env file if present (never commit .env to git).
# Uses setdefault so that values already in os.environ (e.g. pre-loaded by
# bot.py's --env pre-parser) are not overwritten.
def _load_env(path: str | None = None) -> None:
    if path is None:
        # Honour BOT_ENV_FILE if set by the --env pre-parser in bot.py.
        path = _os.environ.get("BOT_ENV_FILE", ".env.test")
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

# Any overrides in this config_test file prioritize completing many short
# order-lifecycle cycles for mechanics validation, not strategy quality.
# Overrides include:
#  - lower MIN_OI_NEAR and MIN_OI_FAR to allow thinly-traded assets (e.g. Far legs) to participate in the test.
#  - shorten FAR_DAYS_OPTIONS  to bias toward short holds.
#  - increase MAX_LEG_SPREAD_PCT to allow wider spreads on thinly-traded assets.
#  - lower MIN_EV to allow any positive EV candidate to be accepted, even if the expected profit is small.

# Assets the bot will trade (scanner, decision engine, execution)
ASSETS = ["BTC","ETH"]

# Assets the data collector will gather option-chain snapshots for.
# Can be a superset of ASSETS — useful for collecting data on assets
# (e.g. SOL) that you want to analyse or backtest without trading them yet.
COLLECTOR_ASSETS = ["BTC", "ETH", "SOL"]

# Calendar horizons (days to expiry)
NEAR_DAYS_OPTIONS = [1, 7, 14]
FAR_DAYS_OPTIONS  = [7, 14]

# 1-day near legs are only valid with short far legs — a 1d/30d+ spread is
# unusual and almost always illiquid.  Set to 0 to disable the restriction.
MAX_FAR_DAYS_FOR_1D_NEAR = 14

# Entry filters
MIN_IV_CONTANGO = 0.02   # front IV must exceed back IV by at least 2%
MIN_POP         = 0.45   # minimum probability of profit
MIN_OI_NEAR     = 100    # minimum open interest on near-leg strike
MIN_OI_FAR      = 50     # minimum open interest on far-leg strike
MIN_EV          = 0      # minimum expected value as a fraction of net_debit.
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
MAX_LEG_SPREAD_PCT     = 0.1    # reject if (ask-bid)/mid > 10% on either leg
MAX_ENTRY_PREMIUM      = 0.10   # reject if net_debit > spread_mid * (1 + 10%)
COMBO_FILL_TIMEOUT_SEC = 30     # seconds to wait for combo fill before individual-leg fallback

# Position sizing
MAX_LOSS_PCT       = 0.02   # max 2% of portfolio per trade
MAX_POSITIONS      = 5      # max concurrent open calendar spreads
MAX_TOTAL_RISK_PCT = 0.1    # hard 10% total capital-at-risk across all open positions
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
SMTP_FROM           = _os.environ.get("SMTP_FROM", "") or SMTP_USER  # sender address (defaults to SMTP_USER)

# ══════════════════════════════════════════════════════════════════════════════
# Parity backfill (Phase 21f) — every key below mirrors config.py exactly.
# config_test.py is a full standalone config exec'd into config.py's namespace,
# so these must be present explicitly (not silently inherited) to keep this file
# honest about what the test-mode instance actually runs with.  Values match
# config.py's defaults unless a deliberate, commented test-mode override is made.
# ══════════════════════════════════════════════════════════════════════════════

# ── Cross Portfolio Margin (X:PM) entry gate (Phase 17) ───────────────────────
MAX_MARGIN_UTILIZATION_PCT = 0.80   # ceiling on maintenance_margin / equity
MARGIN_GATE_ENABLED        = True   # kill switch for the whole gate
MARGIN_GATE_REQUIRED_LIVE  = True   # test/live: missing margin data blocks entry (fail closed)

# ── Phase 21 — deep-ITM/OTM churn guards ──────────────────────────────────────
EV_SCORE_RANKING_CAP        = 2.0    # above-cap ev_scores are demoted in ranking
MAX_MONEYNESS_PCT           = 0.15   # reject strikes > 15% from spot
MARKET_SV_REQUIRE_TWO_SIDED = True   # require bid>0 AND ask>0 before trusting a live mark
CLOSE_CONFIRM_TICKS         = 2      # consecutive stop/TP ticks required before closing
REENTRY_COOLDOWN_SEC        = 1800   # block re-entry of a just-auto-closed instrument

# ── Logging (Phase 20a) ───────────────────────────────────────────────────────
LOG_LEVEL          = "INFO"
LOG_FORMAT         = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
LOG_DATE_FORMAT    = "%Y-%m-%d %H:%M:%S"
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT   = 5
LOG_DIR            = "logs"
NOISY_LOGGERS = {
    "httpx":                       "WARNING",
    "httpcore":                    "WARNING",
    "telegram.ext.Updater":        "WARNING",
    "telegram.vendor.ptb_urllib3": "WARNING",
}
LOG_LEVEL_OVERRIDES = {
    "strategy.decision": "DEBUG",
    "strategy.sizer":    "DEBUG",
}

# ── Network / timeout / retry constants (Phase 20b + 20c) ─────────────────────
DERIBIT_WS_PING_INTERVAL = 20
DERIBIT_WS_PING_TIMEOUT  = 20
DERIBIT_WS_OPEN_TIMEOUT  = 15
DERIBIT_WS_MAX_SIZE      = 10 * 1024 * 1024
RPC_TIMEOUT_SEC          = 15

SLIPPAGE_LIMIT_PCT = 0.02
ORDER_TIMEOUT_SEC  = 30
MAX_ORDER_RETRIES  = 3
ORDER_RETRY_DELAYS = [1, 3, 9]

STUCK_ORDER_TIMEOUT_SEC = 120

ALERT_COOLDOWN_SEC   = 300
SMTP_TIMEOUT_SEC     = 10
TELEGRAM_TIMEOUT_SEC = 10

COLLECTOR_INTERVAL_SEC = 300

# ── Business-logic thresholds (Phase 20e) ─────────────────────────────────────
STRIKE_INCREMENT_TABLE = [
    (5,     0.50),
    (20,    1.0),
    (100,   5.0),
    (500,   10.0),
    (2_000, 50.0),
]
STRIKE_INCREMENT_DEFAULT = 100.0

FAR_LEG_SPREAD_TABLE = [
    (7,  0.005),
    (14, 0.010),
    (30, 0.015),
]
FAR_LEG_SPREAD_DEFAULT            = 0.025
FAR_LEG_LIQUIDITY_PENALTY_PER_30D = 0.005

NEAR_DAY_TOLERANCE = 3
FAR_DAY_TOLERANCE  = 7
EV_SAMPLE_COUNT    = 40

BREAKEVEN_SCAN_STEPS = 800
BREAKEVEN_SCAN_RANGE = (0.50, 1.50)

SPREAD_WARN_PCT = 0.70

ROLL_TRIGGER_DAYS          = 2
POSITION_FAILURE_RETRY_CAP = 3

MIN_CONTRACT_SIZE      = 0.1
STRIKE_CORRELATION_PCT = 0.05

RECONCILE_THRESHOLD_PCT = 0.10
INITIAL_CAPITAL         = 10_000.0
DEFAULT_PORTFOLIO_VALUE = 10_000.0

# ── Paths / timezone / date format (Phase 20f) ────────────────────────────────
from pathlib import Path as _Path
DB_PATH = _Path(_os.environ.get("BOT_DB_PATH", str(_Path(__file__).parent / "db" / "calendar_bot.db")))
HISTORIC_DATA_DB_PATH = _Path(__file__).parent / "backtest" / "historic_data" / "options.duckdb"
TIMEZONE    = "Australia/Sydney"
DATE_FORMAT = "%Y-%m-%d"
