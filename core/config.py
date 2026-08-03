import os
from dotenv import load_dotenv



load_dotenv()


MONGO_URI = os.getenv("MONGO_URL")
MONGO_DB = os.getenv("MONGO_DB")
TOKENS_COLLECTION = os.getenv("TOKENS_COLLECTION")
REFRESH_INTERVAL_MINUTES = 60



# Instrument & Subscription Configuration
MAIN_NIFTY_SECURITY = "NSE_INDEX|Nifty 50"
STRIKE_FROM = 23000.0
STRIKE_TO = 25000.0

# Upstox WebSocket feed mode ("ltpc" or "full")
WEBSOCKET_FEED_MODE = "full"
