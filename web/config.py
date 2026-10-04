import os

WEB_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
REFRESH_STATUS_FILE = os.path.join("data", "runtime", "refresh_status.json")
ORDERS_CACHE_FILE = os.path.join("data", "runtime", "orders_cache.json")
LOGS_DIR = os.getenv("LOG_DIR", "logs")
APP_NAME = os.getenv("APP_NAME", "UpstoxAppV2")