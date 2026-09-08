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
    logger.info(
        "Connecting to MongoDB database=%s",
        settings.mongodb_database,
    )

    mongo.client = AsyncMongoClient(
        settings.mongodb_uri,
        serverSelectionTimeoutMS=5000,
    )

    await mongo.client.admin.command("ping")

    mongo.database = mongo.client[settings.mongodb_database]

    request_collection = get_order_requests_collection()

    await request_collection.create_index([("received_at", DESCENDING)])

    await request_collection.create_index(
        [
            ("type", ASCENDING),
            ("instrument_key", ASCENDING),
        ]
    )

    await request_collection.create_index([("ema_event.timestamp_ms", DESCENDING)])

    execution_collection = get_order_executions_collection()

    await execution_collection.create_index(
        [("document_date", ASCENDING)],
        unique=True,
    )

    await execution_collection.create_index([("orders.event_id", ASCENDING)])

    await execution_collection.create_index([("orders.order_status", ASCENDING)])

    await execution_collection.create_index([("orders.created_at", DESCENDING)])

    logger.info("MongoDB connection established and indexes are ready")


async def close_mongo_connection() -> None:
    if mongo.client is not None:
        await mongo.client.close()

        logger.info("MongoDB connection closed")

    mongo.client = None
    mongo.database = None


def get_order_requests_collection() -> AsyncCollection:
    if mongo.database is None:
        raise RuntimeError("MongoDB is not connected")

    return mongo.database[settings.mongodb_collection]


def get_order_executions_collection() -> AsyncCollection:
    if mongo.database is None:
        raise RuntimeError("MongoDB is not connected")

    return mongo.database["order_execs"]


def get_upstox_tokens_collection() -> AsyncCollection:
    if mongo.client is None:
        raise RuntimeError("MongoDB is not connected")

    token_database = mongo.client[settings.upstox_mongodb_database]

    return token_database[settings.upstox_tokens_collection]
