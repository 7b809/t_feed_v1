import os
from typing import Optional

from dotenv import load_dotenv
from pymongo import MongoClient


def load_upstox_access_token(
    env_path: str = ".env",
    mongo_url_var: str = "MONGO_URL",
    db_name_var: str = "UPSTOX_APP_V2",
    collection_name: str = "app_tokens",
    doc_id: str = "upstox_access_token",
) -> str:
    """
    Loads the Upstox access token from MongoDB using config from a .env file.
    Does not log, print, or write the token to disk.
    """
    load_dotenv(dotenv_path=env_path, override=False)

    mongo_url: Optional[str] = os.getenv(mongo_url_var)
    if not mongo_url:
        raise ValueError(f"Missing required environment variable: {mongo_url_var}")

    db_name: str = os.getenv(db_name_var, db_name_var)

    client: Optional[MongoClient] = None
    try:
        client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
        collection = client[db_name][collection_name]

        doc = collection.find_one(
            {"_id": doc_id},
            projection={"_id": 0, "access_token": 1},
        )
        if not doc or not doc.get("access_token"):
            raise LookupError(
                f"No valid access_token found in {db_name}.{collection_name} for _id={doc_id!r}."
            )

        token = doc["access_token"]
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Retrieved access_token is empty or not a string.")

        return token.strip()
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    token = load_upstox_access_token()
    print(f"Token loaded successfully (length: {len(token)} chars).")
