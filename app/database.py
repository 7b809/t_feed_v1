import logging
from pymongo import MongoClient
from app.config import settings

logger = logging.getLogger("uvicorn")


class Database:
    client: MongoClient = None
    db = None


db_instance = Database()


class AppTokenState:
    access_token: str | None = None
    updated_at: str | None = None


token_state = AppTokenState()


def connect_to_mongo():
    """Establishes connection to MongoDB synchronously."""
    logger.info("Connecting to MongoDB via PyMongo...")
    db_instance.client = MongoClient(settings.MONGO_URL)
    db_instance.db = db_instance.client[settings.MONGO_DB]
    logger.info("MongoDB connected successfully.")


def close_mongo_connection():
    """Closes MongoDB connection synchronously."""
    logger.info("Closing MongoDB connection...")
    if db_instance.client:
        db_instance.client.close()
        logger.info("MongoDB connection closed.")


def load_upstox_token():
    """Fetches the single access token document on startup and stores it in memory."""
    try:
        collection = db_instance.db[settings.TOKENS_COLLECTION]
        # Direct PyMongo call (no await needed)
        doc = collection.find_one({"_id": "upstox_access_token"})

        if doc and "access_token" in doc:
            token_state.access_token = doc.get("access_token")
            token_state.updated_at = str(doc.get("updated_at"))
            logger.info("Successfully loaded Upstox Access Token into memory.")
        else:
            logger.warning(
                "Token document '_id: upstox_access_token' not found in collection."
            )
    except Exception as e:
        logger.error(f"Failed to load token from MongoDB: {e}")
