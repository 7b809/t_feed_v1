"""Dedicated member identity database and signed session helpers."""
from __future__ import annotations
import hashlib, hmac, os, re, secrets, time
from datetime import datetime, timezone
from typing import Any
from fastapi import HTTPException, Request
from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError
from core import config

_DB_NAME = os.getenv("USER_AUTH_MONGO_DB", "MARKET_ENGINE_IDENTITY").strip()
_USERS = os.getenv("USER_AUTH_USERS_COLLECTION", "users").strip()
_AUDIT = os.getenv("USER_AUTH_AUDIT_COLLECTION", "auth_audit").strip()
_COOKIE = os.getenv("USER_AUTH_COOKIE_NAME", "market_member")
_TTL = max(900, int(os.getenv("USER_AUTH_SESSION_TTL_SECONDS", "28800")))
_ITERATIONS = 310_000
_client = _db = None

def _now():
    return datetime.now(timezone.utc)

def _database():
    global _client, _db
    if _db is None:
        uri = getattr(config, "MONGO_URI", None) or os.getenv("MONGO_URL") or os.getenv("MONGO_URI")
        if not uri:
            raise RuntimeError("MONGO_URI/MONGO_URL is required for member accounts")
        _client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        _db = _client[_DB_NAME]
        _db[_USERS].create_index([("email", ASCENDING)], unique=True)
        _db[_USERS].create_index([("status", ASCENDING), ("created_at", ASCENDING)])
        _db[_AUDIT].create_index([("created_at", ASCENDING)])
    return _db

def _audit(event, email=None, detail=None):
    try:
        _database()[_AUDIT].insert_one({"event": event, "email": email, "detail": detail or {}, "created_at": _now()})
    except Exception:
        pass

def _email(value):
    value = (value or "").strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise HTTPException(422, "Enter a valid email address")
    return value

def _hash(password, salt=None):
    if len(password) < 12 or len(password) > 128:
        raise HTTPException(422, "Password must contain 12 to 128 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return "pbkdf2_sha256$" + str(_ITERATIONS) + "$" + salt.hex() + "$" + digest.hex()

def _verify(password, encoded):
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)).hex()
        return hmac.compare_digest(candidate, digest_hex)
    except Exception:
        return False

def register_user(name, email, password):
    name = " ".join((name or "").split())
    if not name or len(name) > 100:
        raise HTTPException(422, "Name is required (maximum 100 characters)")
    email = _email(email)
    doc = {"name": name, "email": email, "password_hash": _hash(password),
           "role": "member", "status": "pending", "created_at": _now(),
           "updated_at": _now(), "last_login_at": None, "session_version": 1}
    try:
        result = _database()[_USERS].insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "An account with this email already exists")
    _audit("registration_submitted", email)
    return {"user_id": str(result.inserted_id), "email": email, "status": "pending"}

def authenticate_user(email, password):
    email = _email(email)
    user = _database()[_USERS].find_one({"email": email})
    if not user or not _verify(password, user.get("password_hash", "")):
        _audit("login_failed", email)
        raise HTTPException(401, "Invalid email or password")
    if user.get("status") != "active":
        _audit("login_blocked", email, {"status": user.get("status")})
        raise HTTPException(403, "Your account is awaiting approval or has been disabled")
    _database()[_USERS].update_one({"_id": user["_id"]}, {"$set": {"last_login_at": _now()}})
    _audit("login_success", email)
    return {"user_id": str(user["_id"]), "email": email, "name": user["name"],
            "role": "member", "session_version": int(user.get("session_version", 1))}

def _secret():
    return (os.getenv("USER_AUTH_SESSION_SECRET") or getattr(config, "ADMIN_SESSION_SECRET", "") or "").strip().encode()

def create_user_session(user):
    secret = _secret()
    if len(secret) < 32:
        raise RuntimeError("Set USER_AUTH_SESSION_SECRET (32+ characters) or ADMIN_SESSION_SECRET")
    expiry = int(time.time()) + _TTL
    payload = (user["user_id"] + ":" + str(expiry) + ":" + str(user["session_version"])).encode()
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return payload.hex() + "." + signature

def get_user_session(request: Request):
    raw = request.cookies.get(_COOKIE, "")
    if not raw or "." not in raw:
        return None
    try:
        payload_hex, signature = raw.split(".", 1)
        payload = bytes.fromhex(payload_hex)
        secret = _secret()
        expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        if len(secret) < 32 or not hmac.compare_digest(signature, expected):
            return None
        user_id, expiry, version = payload.decode().split(":")
        if int(expiry) < int(time.time()):
            return None
        from bson import ObjectId
        user = _database()[_USERS].find_one({"_id": ObjectId(user_id)})
        if not user or user.get("status") != "active" or int(user.get("session_version", 1)) != int(version):
            return None
        return {"user_id": user_id, "email": user["email"], "name": user["name"], "role": "member"}
    except Exception:
        return None

def list_users(status=None, limit=100):
    query = {"status": status} if status else {}
    rows = _database()[_USERS].find(query, {"password_hash": 0}).sort("created_at", -1).limit(max(1, min(limit, 500)))
    output = []
    for row in rows:
        row["user_id"] = str(row.pop("_id"))
        output.append(row)
    return output

def update_user_status(user_id, status):
    if status not in {"active", "suspended", "rejected"}:
        raise HTTPException(422, "Status must be active, suspended or rejected")
    from bson import ObjectId
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(422, "Invalid user id")
    result = _database()[_USERS].update_one({"_id": oid},
        {"$set": {"status": status, "updated_at": _now()}, "$inc": {"session_version": 1}})
    if not result.matched_count:
        raise HTTPException(404, "User not found")
    user = _database()[_USERS].find_one({"_id": oid}, {"password_hash": 0})
    _audit("account_status_changed", user.get("email"), {"status": status})
    user["user_id"] = str(user.pop("_id"))
    return user

def revoke_user_sessions(user_id):
    from bson import ObjectId
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(422, "Invalid user id")
    result = _database()[_USERS].update_one({"_id": oid},
        {"$inc": {"session_version": 1}, "$set": {"updated_at": _now()}})
    if not result.matched_count:
        raise HTTPException(404, "User not found")
    _audit("sessions_revoked", user_id)
    return {"user_id": user_id, "sessions_revoked": True}
