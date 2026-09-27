import os
import json
from pymongo import MongoClient
from bson import json_util
from dotenv import load_dotenv

# Load .env
load_dotenv()

# =========================
# Configuration
# =========================

MONGO_URI = os.getenv("MONGO_ATLAS_URL")

DB_NAME = "UPSTOX_APP"
COLLECTION_NAME = "isolated_instrumentevent"

OUTPUT_FILE = "isolated_instrumentevent.json"


# =========================
# Validate environment
# =========================

if not MONGO_URI:
    raise ValueError(
        "MONGO_ATLAS_URL is not configured in the .env file"
    )


# =========================
# Connect MongoDB
# =========================

print("Connecting to MongoDB...")

client = MongoClient(MONGO_URI)

db = client[DB_NAME]
collection = db[COLLECTION_NAME]


# =========================
# Load collection
# =========================

print(f"Loading collection: {DB_NAME}.{COLLECTION_NAME}")

documents = list(collection.find({}))

print(f"Documents found: {len(documents)}")


# =========================
# Save JSON
# =========================

with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
    json.dump(
        documents,
        file,
        default=json_util.default,
        indent=2,
        ensure_ascii=False
    )


# =========================
# Done
# =========================

print(f"Successfully exported collection.")
print(f"Output file: {OUTPUT_FILE}")

client.close()