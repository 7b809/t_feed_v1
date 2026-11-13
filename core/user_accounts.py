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
_SECURITY_INCIDENTS = os.getenv("USER_AUTH_SECURITY_INCIDENTS_COLLECTION", "security_incidents").strip()
_COOKIE = os.getenv("USER_AUTH_COOKIE_NAME", "market_member")
_TTL = max(900, int(os.getenv("USER_AUTH_SESSION_TTL_SECONDS", "28800")))
_MAX_FAILED = max(1, int(os.getenv("USER_AUTH_MAX_FAILED_LOGINS", "3")))
_LOCK_MINUTES = max(1, int(os.getenv("USER_AUTH_LOCK_MINUTES", "120")))
_ITERATIONS = 310_000
_client = _db = None

def _now():
    return datetime.now(timezone.utc)

def _utc(value):
    """MongoDB may return naive UTC datetimes; normalize before comparing."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

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
        _db[_SECURITY_INCIDENTS].create_index([("user_id", ASCENDING), ("created_at", -1)])
        _db[_SECURITY_INCIDENTS].create_index([("created_at", ASCENDING)])
    return _db

def _audit(event, email=None, detail=None):
    try:
        _database()[_AUDIT].insert_one({"event": event, "email": email, "detail": detail or {}, "created_at": _now()})
    except Exception as exc:
        from core.logger import get_logger
        get_logger(__file__).exception("MongoDB auth audit write failed: %s", event)
        raise RuntimeError("Unable to persist authentication audit event") from exc


def _notify_security_incident(email, name, detail):
    """Notify the configured administrator about a member security incident."""
    enabled = os.getenv("USER_AUTH_SECURITY_TELEGRAM_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    token = (getattr(config, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = (getattr(config, "TELEGRAM_CHAT_ID", "") or "").strip()
    if not enabled or not token or not chat_id:
        return False
    try:
        import requests
        from html import escape
        message = (
            "🚨 <b>NG7 member security alert</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Member:</b> {escape(str(name or 'Unknown'))}\n"
            f"✉️ <b>Email:</b> {escape(str(email or 'Unknown'))}\n"
            f"⚠️ <b>Event:</b> {escape(str(detail.get('reason') or 'Suspicious browser activity'))}\n"
            f"🌐 <b>Client:</b> {escape(str(detail.get('client_ip') or 'Unknown'))}\n"
            f"🧭 <b>Path:</b> {escape(str(detail.get('path') or 'Unknown'))}\n"
            "🔒 <b>Action:</b> Member session revoked and user logged out.\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Review the member account in the NG7 Admin Panel."
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
        get_logger(__file__).warning("Security incident Telegram alert failed (%s)", type(exc).__name__)
        return False


def flag_user_security_incident(user_id, details=None):
    """Flag a member and revoke every active session after a security incident."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId(str(user_id))
    except (InvalidId, TypeError):
        return False

    details = dict(details or {})
    now = _now()
    collection = _database()[_USERS]
    user = collection.find_one({"_id": oid}, {"name": 1, "email": 1, "session_version": 1})
    if not user:
        return False

    new_version = int(user.get("session_version", 1)) + 1
    reason = str(details.get("reason") or "Suspicious browser inspection activity")[:200]
    safe_details = {
        "user_id": str(oid),
        "reason": reason,
        "path": str(details.get("path") or "")[:300],
        "client_ip": str(details.get("client_ip") or "")[:100],
        "user_agent": str(details.get("user_agent") or "")[:500],
        "session_revoked": True,
    }
    collection.update_one(
        {"_id": oid},
        {"$set": {
            "security_flagged": True,
            "last_security_incident_at": now,
            "last_security_incident_reason": reason,
            "security_incident_version": new_version,
            "updated_at": now,
        }, "$inc": {
            "session_version": 1,
            "security_incident_count": 1,
        }},
    )
    _database()[_SECURITY_INCIDENTS].insert_one({**safe_details, "email": user.get("email"), "name": user.get("name"), "event": "security_devtools_detected", "created_at": now, "review_status": "flagged"})
    _audit("security_devtools_detected", user.get("email"), safe_details)
    _notify_security_incident(user.get("email"), user.get("name"), safe_details)
    return True


def get_security_revocation_notice(request):
    """Return True when this cookie was revoked by a recorded security incident."""
    raw = request.cookies.get(_COOKIE, "")
    if not raw or "." not in raw:
        return False
    try:
        payload_hex, signature = raw.split(".", 1)
        payload = bytes.fromhex(payload_hex)
        secret = _secret()
        expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        if len(secret) < 32 or not hmac.compare_digest(signature, expected):
            return False
        user_id, expiry, version = payload.decode().split(":")
        if int(expiry) < int(time.time()):
            return False
        from bson import ObjectId
        user = _database()[_USERS].find_one(
            {"_id": ObjectId(user_id)},
            {"session_version": 1, "security_flagged": 1, "security_incident_version": 1},
        )
        if not user or not user.get("security_flagged"):
            return False
        current_version = int(user.get("session_version", 1))
        incident_version = int(user.get("security_incident_version", -1))
        return current_version != int(version) and incident_version == current_version
    except Exception:
        return False

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
        "name": name, "username": name.strip().lower(), "email": email, "password_hash": _hash(password),
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

def authenticate_user(identifier, password):
    """Authenticate by registered email or account name (username)."""
    identifier = (identifier or "").strip()
    if not identifier or len(identifier) > 254:
        raise HTTPException(422, "Enter your username or email address")
    normalized = identifier.lower()
    collection = _database()[_USERS]
    user = collection.find_one({"$or": [{"email": normalized}, {"username": normalized}, {"name": {"$regex": "^" + re.escape(identifier) + "$", "$options": "i"}}]})
    audit_identity = normalized
    if not user:
        _audit("login_failed", audit_identity, {"identifier_type": "username_or_email"})
        raise HTTPException(401, "Invalid email or password")

    if user.get("login_enabled") is False:
        _audit("login_blocked_manual", audit_identity)
        raise HTTPException(403, "Login is disabled by an administrator. Contact support.")

    # Check an existing lock before password verification. Repeated attempts
    # during the lock window must not extend the two-hour timer.
    lock_until = user.get("lock_until")
    if lock_until and _utc(lock_until) > _now():
        _audit("login_blocked_locked", audit_identity, {"lock_until": _utc(lock_until).isoformat()})
        raise HTTPException(423, f"Too many failed attempts. Login is locked until {_utc(lock_until).strftime('%Y-%m-%d %H:%M UTC')}.")

    if not _verify(password, user.get("password_hash", "")):
        attempts = int(user.get("failed_login_attempts", 0)) + 1
        update = {"failed_login_attempts": attempts, "updated_at": _now()}
        if attempts >= _MAX_FAILED:
            new_lock_until = _now() + timedelta(minutes=_LOCK_MINUTES)
            update["lock_until"] = new_lock_until
            _audit("login_temporarily_locked", audit_identity, {"attempts": attempts, "lock_until": new_lock_until.isoformat()})
        collection.update_one({"_id": user["_id"]}, {"$set": update})
        _audit("login_failed", audit_identity, {"attempts": attempts})
        raise HTTPException(401, "Invalid email or password")

    if lock_until and _utc(lock_until) <= _now():
        collection.update_one({"_id": user["_id"]}, {"$set": {"failed_login_attempts": 0, "lock_until": None}})
    if user.get("status") != "active":
        _audit("login_blocked", audit_identity, {"status": user.get("status")})
        raise HTTPException(403, "Your account is awaiting approval or has been disabled")
    collection.update_one({"_id": user["_id"]}, {"$set": {
        "last_login_at": _now(), "failed_login_attempts": 0, "lock_until": None, "updated_at": _now()
    }})
    _audit("login_success", user.get("email"), {"identifier_type": "username_or_email"})
    return {"user_id": str(user["_id"]), "email": user["email"], "name": user["name"],
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
        if user.get("lock_until") and _utc(user["lock_until"]) > _now():
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
        row["temporarily_locked"] = bool(lock_until and _utc(lock_until) > now)
        row["suspicious"] = (
            row["temporarily_locked"]
            or int(row.get("failed_login_attempts", 0)) >= _MAX_FAILED
            or bool(row.get("security_flagged"))
            or int(row.get("security_incident_count", 0)) > 0
        )
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


def update_user_profile(user_id, name=None, email=None):
    oid = _object_id(user_id)
    updates = {"updated_at": _now()}
    if name is not None:
        clean_name = " ".join(str(name).split())
        if not clean_name or len(clean_name) > 100:
            raise HTTPException(422, "Name is required (maximum 100 characters)")
        updates["name"] = clean_name
    if email is not None:
        updates["email"] = _email(email)
    if len(updates) == 1:
        raise HTTPException(422, "Provide a name or email to update")
    try:
        result = _database()[_USERS].update_one({"_id": oid}, {"$set": updates})
    except DuplicateKeyError:
        raise HTTPException(409, "Another account already uses this email")
    if not result.matched_count:
        raise HTTPException(404, "User not found")
    user = _database()[_USERS].find_one({"_id": oid}, {"password_hash": 0})
    _audit("account_profile_updated", user.get("email"))
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

 
def clear_user_security_flag(user_id, admin_actor="admin"):
    """Clear active security flag and reset visible counters, preserving incident history."""
    oid = _object_id(user_id)
    now = _now()
    collection = _database()[_USERS]
    user = collection.find_one({"_id": oid}, {"name": 1, "email": 1})
    if not user:
        raise HTTPException(404, "User not found")
    result = collection.update_one({"_id": oid}, {"$set": {
        "security_flagged": False,
        "security_incident_count": 0,
        "failed_login_attempts": 0,
        "lock_until": None,
        "security_review_cleared_at": now,
        "security_review_cleared_by": str(admin_actor)[:120],
        "updated_at": now,
    }, "$inc": {"session_version": 1}})
    if not result.matched_count:
        raise HTTPException(404, "User not found")
    detail = {"user_id": user_id, "admin_actor": str(admin_actor)[:120],
              "incident_history_preserved": True, "failed_login_attempts_reset": True}
    _database()[_SECURITY_INCIDENTS].insert_one({
        **detail, "email": user.get("email"), "name": user.get("name"),
        "event": "security_flag_cleared", "created_at": now, "review_status": "cleared",
    })
    _audit("security_flag_cleared", user.get("email"), detail)
    return {"user_id": user_id, "security_flagged": False, "security_incident_count": 0,
            "failed_login_attempts": 0, "incident_history_preserved": True}
