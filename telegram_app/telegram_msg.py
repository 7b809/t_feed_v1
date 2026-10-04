"""
telegram_app/telegram_msg.py

Low-level telegram transport via plain HTTP (urllib). No python telegram
package is used. Every call is defensive: network errors are swallowed and
logged as short, targeted summaries so a dead telegram endpoint never
crashes the app.

Master switch
-------------
Every public function here starts with the same guard:

    if not _active():
        return <no-op return value>

`_active()` combines:
  - TELEGRAM_ENABLED master switch (from telegram_config)
  - Presence of BOT_TOKEN and CHAT_ID

When the master switch is off, `_active()` returns False and every call
becomes a no-op. Nothing is sent, nothing is polled, nothing is deleted,
nothing is logged at ERROR level. The rest of the application can call
these functions unconditionally without any additional checks.

Public API
----------
send_message()          -> Optional[int]   (message_id or None)
delete_message()        -> bool
get_updates()           -> Optional[dict]
flush_pending_updates() -> Optional[int]   (highest update_id or None)
"""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from core.logger import get_logger
from telegram_app.config import telegram_config

logger = get_logger(__name__)

_send_lock = threading.Lock()


# ---------------------------------------------------------------------------
# master switch
# ---------------------------------------------------------------------------


def _active() -> bool:
    """
    Single point of truth for "should we talk to Telegram right now?".

    telegram_config.enabled already folds in the master switch
    (TELEGRAM_ENABLED) and the presence of BOT_TOKEN + CHAT_ID. We wrap
    it here so the intent is explicit at every call site and so the
    behaviour can be overridden in one place if needed later.
    """
    try:
        return bool(telegram_config.enabled)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "telegram_msg::_active failed | step=read config | reason=%s",
            f"{type(exc).__name__}: {str(exc)[:120]}",
        )
        return False


# ---------------------------------------------------------------------------
# low-level HTTP helpers
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: Dict[str, Any], timeout: int) -> Optional[Dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "raw": raw[:500]}


def _get_json(url: str, timeout: int) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "raw": raw[:500]}


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def send_message(
    text: str,
    parse_mode: str = "HTML",
    chat_id: Optional[str] = None,
) -> Optional[int]:
    """
    Send a message to the configured chat (or an explicit chat_id).

    Returns the new message_id on success, None on any failure.
    Never raises. No-op when the master switch is off.
    """
    if not _active():
        return None
    if not text:
        return None

    target_chat = chat_id or telegram_config.CHAT_ID
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        with _send_lock:
            result = _post_json(
                telegram_config.send_url(),
                payload,
                timeout=telegram_config.HTTP_TIMEOUT_SEC,
            )
        if result and result.get("ok"):
            msg = result.get("result") or {}
            message_id = msg.get("message_id")
            return int(message_id) if message_id is not None else None
        reason = (result or {}).get("description", "unknown")
        logger.error(
            "telegram_msg::send_message failed | step=POST sendMessage | reason=%s",
            str(reason)[:200],
        )
        return None
    except urllib.error.HTTPError as exc:
        logger.error(
            "telegram_msg::send_message failed | step=POST sendMessage | reason=HTTP %s",
            exc.code,
        )
        return None
    except urllib.error.URLError as exc:
        logger.error(
            "telegram_msg::send_message failed | step=POST sendMessage | reason=URLError %s",
            str(exc.reason)[:200],
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "telegram_msg::send_message failed | step=POST sendMessage | reason=%s",
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return None


def delete_message(
    message_id: int,
    chat_id: Optional[str] = None,
) -> bool:
    """
    Best-effort delete of a single message. Returns True on success.

    Silently returns False if the message is missing, too old, if
    Telegram rejects the call, or if the master switch is off.
    Never raises.
    """
    if not _active() or not message_id:
        return False

    target_chat = chat_id or telegram_config.CHAT_ID
    params = urllib.parse.urlencode(
        {"chat_id": target_chat, "message_id": int(message_id)}
    )
    url = f"{telegram_config.API_BASE}/bot{telegram_config.BOT_TOKEN}/deleteMessage?{params}"

    try:
        result = _get_json(url, timeout=telegram_config.HTTP_TIMEOUT_SEC)
        if result and result.get("ok"):
            return True
        reason = (result or {}).get("description", "unknown")
        logger.debug(
            "telegram_msg::delete_message skipped | message_id=%s | reason=%s",
            message_id, str(reason)[:160],
        )
        return False
    except urllib.error.HTTPError as exc:
        logger.debug(
            "telegram_msg::delete_message skipped | message_id=%s | reason=HTTP %s",
            message_id, exc.code,
        )
        return False
    except urllib.error.URLError as exc:
        logger.debug(
            "telegram_msg::delete_message skipped | message_id=%s | reason=URLError %s",
            message_id, str(exc.reason)[:160],
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "telegram_msg::delete_message skipped | message_id=%s | reason=%s",
            message_id, f"{type(exc).__name__}: {str(exc)[:160]}",
        )
        return False


def get_updates(offset: Optional[int], timeout_sec: int) -> Optional[Dict[str, Any]]:
    """
    Long-poll getUpdates. Returns the parsed JSON body or None on failure.
    Returns None immediately when the master switch is off.
    """
    if not _active():
        return None

    url = telegram_config.updates_url()
    if offset is not None:
        url = f"{url}?offset={offset}&timeout={timeout_sec}"
    else:
        url = f"{url}?timeout={timeout_sec}"

    try:
        return _get_json(url, timeout=timeout_sec + telegram_config.HTTP_TIMEOUT_SEC)
    except urllib.error.HTTPError as exc:
        logger.error(
            "telegram_msg::get_updates failed | step=GET getUpdates | reason=HTTP %s",
            exc.code,
        )
        return None
    except urllib.error.URLError as exc:
        logger.error(
            "telegram_msg::get_updates failed | step=GET getUpdates | reason=URLError %s",
            str(exc.reason)[:200],
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "telegram_msg::get_updates failed | step=GET getUpdates | reason=%s",
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return None


def flush_pending_updates() -> Optional[int]:
    """
    Drain any updates queued while the app was offline and return the
    highest update_id seen, or None if the buffer was empty.

    Uses offset=-1 to fetch the most recent update only. Telegram then
    treats every update with a smaller id as acknowledged once the caller
    polls with `offset = last_id + 1`.

    Returns None immediately when the master switch is off, so callers
    do not need to guard this call.
    """
    if not _active():
        return None

    url = f"{telegram_config.updates_url()}?offset=-1&timeout=0&limit=1"
    try:
        body = _get_json(url, timeout=telegram_config.HTTP_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "telegram_msg::flush_pending_updates failed | step=GET getUpdates | reason=%s",
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return None

    if not body or not body.get("ok"):
        reason = (body or {}).get("description", "unknown")
        logger.error(
            "telegram_msg::flush_pending_updates failed | step=parse | reason=%s",
            str(reason)[:200],
        )
        return None

    results = body.get("result") or []
    if not results:
        logger.info("telegram_msg::flush_pending_updates | buffer empty")
        return None

    last_id = None
    for update in results:
        uid = update.get("update_id")
        if isinstance(uid, int):
            last_id = uid if last_id is None else max(last_id, uid)

    logger.info(
        "telegram_msg::flush_pending_updates | dropped_backlog | highest_update_id=%s",
        last_id,
    )
    return last_id