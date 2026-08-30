import os


def b(name, default):
    value = os.getenv(name)
    return default if value is None else value.lower() in ("1", "true", "yes", "y", "on")


def f(name, default):
    return float(os.getenv(name, str(default)))


def i(name, default):
    return int(os.getenv(name, str(default)))


# Sandbox-only connection. There is intentionally no live endpoint setting.
APP_KEY = os.getenv("WEBULL_APP_KEY", "")
APP_SECRET = os.getenv("WEBULL_APP_SECRET", "")
ACCOUNT_ID = os.getenv("WEBULL_ACCOUNT_ID", "")
REGION = os.getenv("WEBULL_REGION", "us")
DRY_RUN = b("DRY_RUN", True)
SYMBOL = os.getenv("UNDERLYING_SYMBOL", "SPY")

HISTORY_COUNT = i("HISTORY_COUNT", 300)
POLL_SECONDS = i("POLL_SECONDS", 15)
IDLE_POLL_SECONDS = i("IDLE_POLL_SECONDS", 30)
WEBULL_MIN_REQUEST_INTERVAL = f("WEBULL_MIN_REQUEST_INTERVAL", 1.05)
OPTION_QUANTITY = i("OPTION_QUANTITY", 1)
STATE_PATH = os.getenv("STATE_PATH", "bot_state.json")

# Entry/strategy parameters. These mirror the current Pine-derived build.
KALMAN_Q = f("KALMAN_Q", 0.01)
KALMAN_R = f("KALMAN_R", 0.20)
ST_FACTOR = f("ST_FACTOR", 2.0)
ST_ATR = i("ST_ATR_PERIOD", 7)
WAVE_LEN = i("WAVE_EQUIL_LEN", 50)
BB_LEN = i("BB_LEN", 20)
BB_MULT = f("BB_MULT", 1.5)
ADX_LEN = i("ADX_LEN", 14)
DI_LEN = i("DI_LEN", 14)
ADX_GAIN = f("ADX_GAIN", 0.8)
WAVE_SMOOTH = i("WAVE_SMOOTH_LEN", 10)
OFFSET = f("BASE_OFFSET_MULT", 1.0)
EXPANSION = f("EXPANSION_MULT", 1.0)
COMPRESSION_LEN = i("COMPRESSION_LOOKBACK", 50)
COMPRESSION_PCT = f("COMPRESSION_PCT", 0.30)
STOP_ATR = f("STOP_ATR_MULT", 1.5)
TARGET_ATR = f("TARGET_ATR_MULT", 2.0)
BE_ATR = f("BREAKEVEN_ATR_MULT", 1.0)
USE_BE = b("USE_BREAKEVEN", True)
USE_ZONE = b("USE_ZONE_EXIT", True)
MAX_BARS_IN_TRADE = i("MAX_BARS_IN_TRADE", 12)

# Option selection / execution.
MIN_PREMIUM = f("MIN_OPTION_PREMIUM", 0.10)
MAX_PREMIUM = f("MAX_OPTION_PREMIUM", 8.00)
MAX_SPREAD = f("MAX_SPREAD_PCT", 0.15)
COOLDOWN = i("COOLDOWN_AFTER_TRADE_SECONDS", 60)

# New exit/risk controls. These are deliberately environment-configurable.
USE_OPTION_PREMIUM_RISK = b("USE_OPTION_PREMIUM_RISK", True)
OPTION_STOP_LOSS_PCT = f("OPTION_STOP_LOSS_PCT", 0.35)
OPTION_TAKE_PROFIT_PCT = f("OPTION_TAKE_PROFIT_PCT", 0.50)
OPTION_BREAKEVEN_TRIGGER_PCT = f("OPTION_BREAKEVEN_TRIGGER_PCT", 0.25)
OPTION_BREAKEVEN_FLOOR_PCT = f("OPTION_BREAKEVEN_FLOOR_PCT", 0.00)
MAX_HOLD_MINUTES = i("MAX_HOLD_MINUTES", MAX_BARS_IN_TRADE * 5)

# Session safety. All times are New York exchange time.
TIMEZONE = os.getenv("TRADING_TIMEZONE", "America/New_York")
RTH_START = os.getenv("RTH_START", "09:30")
RTH_END = os.getenv("RTH_END", "16:00")
NO_NEW_ENTRIES_AFTER = os.getenv("NO_NEW_ENTRIES_AFTER", "15:45")
FORCE_EXIT_TIME = os.getenv("FORCE_EXIT_TIME", "15:55")

# Pending-order safety.
ENTRY_ORDER_TIMEOUT_SECONDS = i("ENTRY_ORDER_TIMEOUT_SECONDS", 30)
EXIT_ORDER_TIMEOUT_SECONDS = i("EXIT_ORDER_TIMEOUT_SECONDS", 20)
MAX_EXIT_RETRIES = i("MAX_EXIT_RETRIES", 5)

# State/recovery behavior.
RECOVERY_POLL_SECONDS = i("RECOVERY_POLL_SECONDS", 10)
