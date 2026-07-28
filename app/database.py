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
    """
    Establish MongoDB connection.
    """

    logger.info("Connecting to MongoDB via PyMongo...")

    db_instance.client = MongoClient(settings.MONGO_URL)

    db_instance.db = db_instance.client[settings.MONGO_DB]

    logger.info("MongoDB connected successfully.")


def close_mongo_connection():
    """
    Close MongoDB connection.
    """

    logger.info("Closing MongoDB connection...")

    if db_instance.client:
        db_instance.client.close()

        logger.info("MongoDB connection closed.")


def get_token_document():
    """
    Fetch token document from MongoDB.
    """

    try:

        collection = db_instance.db[settings.TOKENS_COLLECTION]

        return collection.find_one({"_id": "upstox_access_token"})

    except Exception as ex:

        logger.error(f"Failed fetching token document: {ex}")

        return None


def load_upstox_token():
    """
    Load token into memory.
    """

    try:

        doc = get_token_document()

        if not doc:

            logger.warning("Token document not found.")

            return False

        access_token = doc.get("access_token")

        if not access_token:

            logger.warning("Access token missing in MongoDB.")

            return False

        token_state.access_token = access_token
        token_state.updated_at = str(doc.get("updated_at"))

        logger.info("Successfully loaded Upstox Access Token into memory.")

        return True

    except Exception as ex:

        logger.error(f"Failed to load token from MongoDB: {ex}")

        return False


def refresh_token_if_changed():
    """
    Reload token only if MongoDB updated_at
    is different from in-memory updated_at.

    Returns:
        True  -> token changed and reloaded
        False -> no change
    """

    try:

        doc = get_token_document()

        if not doc:
            return False

        mongo_updated_at = str(doc.get("updated_at"))

        memory_updated_at = token_state.updated_at

        # First load
        if not token_state.access_token:

            logger.info("Token not found in memory. Loading...")

            load_upstox_token()

            return True

        # Token updated externally
        if mongo_updated_at != memory_updated_at:

            logger.info("Detected Upstox token change in MongoDB.")

            token_state.access_token = doc.get("access_token")

            token_state.updated_at = mongo_updated_at

            logger.info(f"Token refreshed in memory. " f"updated_at={mongo_updated_at}")

            return True

        return False

    except Exception as ex:

        logger.error(f"Failed checking token updates: {ex}")

        return False


def get_token_status():
    """
    Returns token status for diagnostics.
    """

    return {
        "loaded": bool(token_state.access_token),
        "updated_at": token_state.updated_at,
    }
