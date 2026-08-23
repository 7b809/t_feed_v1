from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from core.config import settings
from core.logger import get_logger

logger = get_logger("database")


class MongoDatabase:
    client: AsyncMongoClient | None = None
    database: AsyncDatabase | None = None


mongo = MongoDatabase()


async def connect_to_mongo() -> None:
    """
    Connect to MongoDB using PyMongo AsyncMongoClient.
    MongoDB URI comes from settings.mongodb_uri.
    """

    logger.info(
        "Connecting to MongoDB database=%s",
        settings.mongodb_database,
    )

    mongo.client = AsyncMongoClient(
        settings.mongodb_uri,
        serverSelectionTimeoutMS=5000,
    )

    # Verify MongoDB connection
    await mongo.client.admin.command("ping")

    # Select database
    mongo.database = mongo.client[settings.mongodb_database]

    # Get order request collection
    collection = get_order_requests_collection()

    # Create indexes
    await collection.create_index([("received_at", DESCENDING)])

    await collection.create_index(
        [
            ("type", ASCENDING),
            ("instrument_key", ASCENDING),
        ]
    )

    await collection.create_index([("ema_event.timestamp_ms", DESCENDING)])

    logger.info("MongoDB connection established and indexes are ready")


async def close_mongo_connection() -> None:
    """
    Close MongoDB connection.
    """

    if mongo.client is not None:
        await mongo.client.close()

        logger.info("MongoDB connection closed")

    mongo.client = None
    mongo.database = None


def get_order_requests_collection() -> AsyncCollection:
    """
    Return order request collection.
    """

    if mongo.database is None:
        raise RuntimeError("MongoDB is not connected")

    return mongo.database[settings.mongodb_collection]
