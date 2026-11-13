"""Dedicated member identity database, login throttling and signed sessions."""
from __future__ import annotations

import hashlib, hmac, os, re, secrets, time
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, Request
from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError
from core import config

_DB_NAME = os.getenv("USER_AUTH_MONGO_DB", "MARKET_ENGINE_IDENTITY").strip()
_USERS = os.getenv("USER_AUTH_USERS_COLLECTION", "users").strip()
_AUDIT = os.getenv("USER_AUTH_AUDIT_COLLECTION", "auth_audit").strip()
_COOKIE = os.getenv("USER_AUTH_COOKIE_NAME", "market_member")
_TTL = max(900, int(os.getenv("USER_AUTH_SESSION_TTL_SECONDS", "28800")))
_MAX_FAILED = max(1, int(os.getenv("USER_AUTH_MAX_FAILED_LOGINS", "3")))
_LOCK_MINUTES = max(1, int(os.getenv("USER_AUTH_LOCK_MINUTES", "120")))
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

def _notify_registration(name, email, user_id):
    """Registration alert is intentionally independent of V5 quiet-mode Telegram notifications."""
    enabled = os.getenv("USER_REGISTRATION_TELEGRAM_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    token = (getattr(config, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = (getattr(config, "TELEGRAM_CHAT_ID", "") or "").strip()
    if not enabled or not token or not chat_id:
        return False
    try:
        import requests
        from html import escape
        message = (
            "🆕 <b>New member registration</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Name:</b> {escape(name)}\n"
            f"✉️ <b>Email:</b> {escape(email)}\n"
            f"🆔 <b>Account:</b> <code>{escape(user_id)}</code>\n"
            "🟠 <b>Status:</b> Pending approval\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Review this request in the Market Engine Admin Panel."
        )
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=8,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        from core.logger import get_logger
        get_logger(__file__).warning("Registration Telegram alert failed (%s)", type(exc).__name__)
        return False

def register_user(name, email, password):
    name = " ".join((name or "").split())
    if not name or len(name) > 100:
        raise HTTPException(422, "Name is required (maximum 100 characters)")
    email = _email(email)
    now = _now()
    doc = {
        "name": name, "email": email, "password_hash": _hash(password),
        "role": "member", "status": "pending", "created_at": now,
        "updated_at": now, "last_login_at": None, "session_version": 1,
        "failed_login_attempts": 0, "lock_until": None, "login_enabled": True,
    }
    try:
        result = _database()[_USERS].insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "An account with this email already exists")
    user_id = str(result.inserted_id)
    _audit("registration_submitted", email, {"user_id": user_id})
    _notify_registration(name, email, user_id)
    return {"user_id": user_id, "name": name, "email": email, "status": "pending"}

def authenticate_user(email, password):
    email = _email(email)
    collection = _database()[_USERS]
    user = collection.find_one({"email": email})
    if not user or not _verify(password, user.get("password_hash", "")):
        if user:
            attempts = int(user.get("failed_login_attempts", 0)) + 1
            update = {"failed_login_attempts": attempts, "updated_at": _now()}
            if attempts >= _MAX_FAILED:
                lock_until = _now() + timedelta(minutes=_LOCK_MINUTES)
                update["lock_until"] = lock_until
                _audit("login_temporarily_locked", email, {"attempts": attempts, "lock_until": lock_until.isoformat()})
            collection.update_one({"_id": user["_id"]}, {"$set": update})
        _audit("login_failed", email)
        raise HTTPException(401, "Invalid email or password")
    lock_until = user.get("lock_until")
    if user.get("login_enabled") is False:
        _audit("login_blocked_manual", email)
        raise HTTPException(403, "Login is disabled by an administrator. Contact support.")
    if lock_until and lock_until > _now():
        _audit("login_blocked_locked", email, {"lock_until": lock_until.isoformat()})
        raise HTTPException(423, f"Too many failed attempts. Login is locked until {lock_until.strftime('%Y-%m-%d %H:%M UTC')}.")
    if lock_until and lock_until <= _now():
        collection.update_one({"_id": user["_id"]}, {"$set": {"failed_login_attempts": 0, "lock_until": None}})
    if user.get("status") != "active":
        _audit("login_blocked", email, {"status": user.get("status")})
        raise HTTPException(403, "Your account is awaiting approval or has been disabled")
    collection.update_one({"_id": user["_id"]}, {"$set": {
        "last_login_at": _now(), "failed_login_attempts": 0, "lock_until": None, "updated_at": _now()
    }})
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
        if not user or user.get("status") != "active" or user.get("login_enabled") is False:
            return None
        if user.get("lock_until") and user["lock_until"] > _now():
            return None
        if int(user.get("session_version", 1)) != int(version):
            return None
        return {"user_id": user_id, "email": user["email"], "name": user["name"], "role": "member"}
    except Exception:
        return None

def list_users(status=None, limit=300):
    query = {"status": status} if status else {}
    rows = _database()[_USERS].find(query, {"password_hash": 0}).sort("created_at", -1).limit(max(1, min(limit, 500)))
    output = []
    now = _now()
    for row in rows:
        row["user_id"] = str(row.pop("_id"))
        lock_until = row.get("lock_until")
        row["temporarily_locked"] = bool(lock_until and lock_until > now)
        row["suspicious"] = row["temporarily_locked"] or int(row.get("failed_login_attempts", 0)) >= _MAX_FAILED
        output.append(row)
    return output

def _object_id(user_id):
    from bson import ObjectId
    try:
        return ObjectId(user_id)
    except Exception:
        raise HTTPException(422, "Invalid user id")

def update_user_status(user_id, status):
    if status not in {"active", "suspended", "rejected"}:
        raise HTTPException(422, "Status must be active, suspended or rejected")
    oid = _object_id(user_id)
    result = _database()[_USERS].update_one({"_id": oid}, {
        "$set": {"status": status, "updated_at": _now()},
        "$inc": {"session_version": 1}
    })
    if not result.matched_count:
        raise HTTPException(404, "User not found")
    user = _database()[_USERS].find_one({"_id": oid}, {"password_hash": 0})
    _audit("account_status_changed", user.get("email"), {"status": status})
    user["user_id"] = str(user.pop("_id"))
    return user

def set_user_login_enabled(user_id, enabled):
    oid = _object_id(user_id)
    update = {"login_enabled": bool(enabled), "updated_at": _now()}
    if enabled:
        update.update({"failed_login_attempts": 0, "lock_until": None})
    result = _database()[_USERS].update_one({"_id": oid}, {
        "$set": update, "$inc": {"session_version": 1}
    })
    if not result.matched_count:
        raise HTTPException(404, "User not found")
    user = _database()[_USERS].find_one({"_id": oid}, {"password_hash": 0})
    _audit("admin_login_enabled" if enabled else "admin_login_disabled", user.get("email"))
    user["user_id"] = str(user.pop("_id"))
    return user

def delete_user(user_id):
    oid = _object_id(user_id)
    user = _database()[_USERS].find_one({"_id": oid}, {"password_hash": 0})
    if not user:
        raise HTTPException(404, "User not found")
    _database()[_USERS].delete_one({"_id": oid})
    _audit("account_deleted", user.get("email"))
    return {"user_id": user_id, "deleted": True}

def revoke_user_sessions(user_id):
    oid = _object_id(user_id)
    result = _database()[_USERS].update_one({"_id": oid}, {
        "$inc": {"session_version": 1}, "$set": {"updated_at": _now()}
    })
    if not result.matched_count:
        raise HTTPException(404, "User not found")
    _audit("sessions_revoked", user_id)
    return {"user_id": user_id, "sessions_revoked": True}
