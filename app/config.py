from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
FILES_DIR = BASE_DIR / "files"
LOG_DIR = BASE_DIR / "logs"
IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Market window — Monday-Friday, inclusive. Asia/Kolkata local time.
# ---------------------------------------------------------------------------
SESSION_START_HOUR = 9
SESSION_START_MINUTE = 0
SESSION_END_HOUR = 15
SESSION_END_MINUTE = 40

# Scheduler tick — how often the loop wakes to check the market gate and
# reconcile subscriptions. In seconds.
SCHEDULER_TICK_SECONDS = 15

# How often the subscriptions APIs are re-fetched. In seconds.
SUBSCRIPTIONS_REFRESH_SECONDS = 900  # 15 min

# Websocket reconnect backoff (fixed, per worker).
RECONNECT_DELAY_SECONDS = 5

# Rolling buffer size sent to the dashboard.
MAX_DASHBOARD_MESSAGES = 250


def sanitize_instrument_key(instrument_key: str) -> str:
    """
    Convert an instrument key into a filesystem-safe slug.

    Example:
        "BSE_FO|1101001"        ->  "BSE_FO__1101001"
        "NSE_INDEX|Nifty 50"    ->  "NSE_INDEX__Nifty_50"
    """
    return (
        instrument_key.replace("|", "__")
        .replace(" ", "_")
        .replace("/", "_")
    )


# ---------------------------------------------------------------------------
# Subscription sources
# ---------------------------------------------------------------------------
# Each feed has:
#   label        — human-readable name shown in the dashboard
#   api_url      — endpoint that lists available instruments
#   dir          — directory under files/ where that feed's JSONL files go
#   wanted       — list of specs. Every item returned by api_url is
#                  matched against each spec using AND-of-fields. An item
#                  is kept if it matches ANY spec.
#
# Spec fields (all optional):
#   instrument_key  exact match
#   underlying      string matched (case-insensitive) against
#                   underlying_key / trading_symbol / underlying.
#                   Multi-word specs like "NIFTY 50" are supported.
#   type            "CE" / "PE" — matched against instrument_type
#   strike          exact numeric match against strike_price
#   strike_min      lower bound (inclusive)
#   strike_max      upper bound (inclusive)
#   expiry          "YYYY-MM-DD" — exact match against expiry
#   mode            "ltpc" / "full" / "option_greeks" / etc.
# ---------------------------------------------------------------------------

SUBSCRIPTIONS = {
    "ema": {
        "label": "EMA Live by Instrument",
        "api_url": "https://ema.novag7.in/api/instruments",
        "dir": FILES_DIR / "ema",
        "wanted": [
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22250},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22250},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22300},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22300},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22350},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22350},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22400},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22400},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22450},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22450},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22500},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22500},
        ],
    },
    "feed": {
        "label": "Market Feed",
        "api_url": "https://feed.novag7.in/api/subscriptions",
        "dir": FILES_DIR / "feed",
        "wanted": [
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22250},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22250},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22300},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22300},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22350},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22350},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22400},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22400},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22450},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22450},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "CE", "strike": 22500},
            {"underlying": "NSE_INDEX|Nifty 50", "type": "PE", "strike": 22500},
        ],
    },
}

