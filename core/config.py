import os
from dotenv import load_dotenv



load_dotenv()


MONGO_URI = os.getenv("MONGO_URL")
MONGO_DB = os.getenv("MONGO_DB")
TOKENS_COLLECTION = os.getenv("TOKENS_COLLECTION")
REFRESH_INTERVAL_MINUTES = 60

# Telegram Notification Configuration
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10"))

# Market timezone configuration
MARKET_TIMEZONE = os.getenv("MARKET_TIMEZONE", "Asia/Kolkata")
MARKET_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"


# Market timezone configuration
MARKET_TIMEZONE = "Asia/Kolkata"
MARKET_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"



# Instrument & Subscription Configuration
MAIN_NIFTY_SECURITY = "NSE_INDEX|Nifty 50"
STRIKE_FROM = 23000.0
STRIKE_TO = 25000.0

# Upstox WebSocket feed mode ("ltpc" or "full")
WEBSOCKET_FEED_MODE = "full"
