import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGO_ATLAS_URL = os.getenv("MONGO_ATLAS_URL")

DB_NAME = "UPSTOX_APP"
TOKEN_COLLECTION = "upstox_tokens"


def get_mongo_db():
    """
    Return the UPSTOX_APP MongoDB database.
    """
    if not MONGO_ATLAS_URL:
        raise ValueError("MONGO_ATLAS_URL is not configured in .env")

    client = MongoClient(MONGO_ATLAS_URL)

    return client[DB_NAME]


def get_upstox_access_token():
    """
    Fetch and return only the Upstox access token.
    """

    db = get_mongo_db()

    document = db[TOKEN_COLLECTION].find_one(
        {"_id": "upstox_access_token"},
        {"access_token": 1, "_id": 0}
    )

    if not document:
        raise ValueError(
            "Upstox access token document not found"
        )

    access_token = document.get("access_token")

    if not access_token:
        raise ValueError(
            "Access token is missing from the document"
        )

    return access_token