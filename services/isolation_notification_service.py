# services/isolation_notification_service.py
"""Fire-and-forget notifications for newly persisted isolation records.

Each configured client receives the complete isolation document as JSON.
The caller only starts daemon worker threads; it never waits for HTTP
responses or client acknowledgements.

Authentication
--------------
A single shared secret (``ISOLATION_NOTIFY_CLIENT_KEY``) is sent on every
outbound POST under the header named by
``ISOLATION_NOTIFY_CLIENT_KEY_HEADER`` (default ``X-Isolation-Key``).
Both the sender (this service) and the receiver (each client) must
configure the **same** value; empty means no key header is sent.

Test path
---------
``send_isolation_test_notification`` is a synchronous variant used only
by ``POST /api/isolation-test``. It builds a static dummy isolation
document, waits for each POST, and returns per-URL ``http_status`` /
``ok`` / ``error`` so an operator can verify reachability and auth
end-to-end. The test payload is marked ``is_test=true`` and
``source="isolation_test_endpoint"`` so it can never be mistaken for a
real isolation event.
"""

from __future__ import annotations

from threading import Thread

import requests

from core.config import (
    ISOLATION_NOTIFY_ENABLED,
    ISOLATION_NOTIFY_CLIENT_URLS,
    ISOLATION_NOTIFY_CLIENT_KEY,
    ISOLATION_NOTIFY_CLIENT_KEY_HEADER,
    ISOLATION_NOTIFY_TIMEOUT_SECONDS,
    TIMEZONE,
)
from core.logger import get_logger

logger = get_logger(__file__)

# One-shot warning so a missing shared key is reported once, not per POST.
_key_warning_logged = False


# --------------------------------------------------------------------------- #
# Headers                                                                     #
# --------------------------------------------------------------------------- #
def _build_headers(key: str | None = None) -> dict:
    """Return the HTTP headers for an outbound isolation POST.

    ``key=None`` falls back to the configured
    ``ISOLATION_NOTIFY_CLIENT_KEY``; an explicit string (including ``""``)
    is used verbatim. The key is only added when non-empty, so receivers
    that require authentication will reject misconfigured payloads — that
    is the correct behaviour.
    """
    effective_key = (
        key if key is not None else ISOLATION_NOTIFY_CLIENT_KEY
    ).strip()

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if effective_key:
        headers[ISOLATION_NOTIFY_CLIENT_KEY_HEADER] = effective_key
    return headers


def _warn_missing_key_once() -> None:
    """Log once per process if notifications are running with no key."""
    global _key_warning_logged
    if _key_warning_logged or ISOLATION_NOTIFY_CLIENT_KEY:
        return
    _key_warning_logged = True
    logger.warning(
        "Isolation notifications are running WITHOUT a shared key "
        "(ISOLATION_NOTIFY_CLIENT_KEY is empty). Clients will receive "
        "payloads with no %s header and — if they enforce auth — will "
        "reject them.",
        ISOLATION_NOTIFY_CLIENT_KEY_HEADER,
    )


# --------------------------------------------------------------------------- #
# Production path (fire-and-forget)                                           #
# --------------------------------------------------------------------------- #
def notify_isolation_clients(record: dict) -> int:
    """Dispatch an isolation document to every configured client.

    Returns the number of notification workers started, not the number of
    clients that successfully received the payload.
    """
    if not ISOLATION_NOTIFY_ENABLED:
        logger.debug("Isolation client notifications disabled")
        return 0

    urls = list(dict.fromkeys(ISOLATION_NOTIFY_CLIENT_URLS))
    if not urls:
        logger.warning(
            "Isolation notifications enabled but no client URLs configured"
        )
        return 0

    _warn_missing_key_once()

    # Snapshot the payload before handing it to background workers.
    payload = dict(record)
    started = 0

    for url in urls:
        worker = Thread(
            target=_post_notification,
            args=(url, payload),
            name="isolation-notify",
            daemon=True,
        )
        try:
            worker.start()
            started += 1
        except Exception:
            logger.exception(
                "Could not start isolation notification worker for %s", url
            )

    logger.info(
        "Isolation notification dispatched to %d/%d configured client(s); "
        "HTTP responses are not awaited (key_header=%s, key_set=%s)",
        started,
        len(urls),
        ISOLATION_NOTIFY_CLIENT_KEY_HEADER,
        bool(ISOLATION_NOTIFY_CLIENT_KEY),
    )
    return started


def _post_notification(url: str, payload: dict) -> None:
    """POST in a daemon thread; never blocks the isolation scan caller."""
    try:
        response = requests.post(
            url,
            json=payload,
            headers=_build_headers(),
            timeout=ISOLATION_NOTIFY_TIMEOUT_SECONDS,
        )
        logger.info(
            "Isolation notification HTTP completed: client=%s status=%s",
            url,
            response.status_code,
        )
    except Exception:
        # Best effort only. No retry or delivery guarantee is implied.
        logger.exception("Isolation notification failed for client=%s", url)


# --------------------------------------------------------------------------- #
# Test path (synchronous, real feedback)                                      #
# --------------------------------------------------------------------------- #
def _static_test_instrument() -> dict:
    """Static, fake instrument used only by /api/isolation-test."""
    return {
        "instrument_key": "NSE_FO|TEST0001",
        "exchange_token": "TEST0001",
        "trading_symbol": "NIFTY 25000 CE 09 NOV 26",
        "short_name": None,
        "name": "NIFTY",
        "strike_price": 25000.0,
        "instrument_type": "CE",
        "expiry": "2026-11-09",
        "lot_size": 65,
        "tick_size": 5.0,
        "segment": "NSE_FO",
        "exchange": "NSE",
        "underlying_symbol": "NIFTY",
    }


def _build_static_test_payload() -> dict:
    """Build a complete isolation-shaped document from static dummy data.

    Uses ``build_dummy_isolation_record`` so the JSON shape matches what
    a real isolation produces. The result is additionally tagged with
    ``is_test=True`` and ``source="isolation_test_endpoint"`` so receivers
    can unambiguously distinguish it from a live isolation.
    """
    # Lazy imports keep module-load order trivial and avoid any risk of
    # circulars via services.dummy_isolation_builder → execution_mode_service.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from services.dummy_isolation_builder import build_dummy_isolation_record

    now = datetime.now(ZoneInfo(TIMEZONE))
    touch_time = now.replace(
        hour=9, minute=30, second=0, microsecond=0
    ).isoformat()

    record = build_dummy_isolation_record(
        instrument=_static_test_instrument(),
        strike=25000,
        bias="CE",
        nifty_ltp=25010.50,
        option_ltp=150.00,
        touch_time_iso=touch_time,
        nifty_ltp_source="test_static",
    )

    # Mark clearly as a test — not a real isolation event.
    record["source"] = "isolation_test_endpoint"
    record["is_test"] = True
    explanation = record.get("explanation")
    if isinstance(explanation, dict):
        explanation["summary"] = (
            "[TEST] " + str(explanation.get("summary", ""))
        )
        explanation["is_test"] = True
    return record


def _post_test_notification(
    url: str, payload: dict, key: str | None
) -> dict:
    """Synchronous POST for /api/isolation-test. Never raises."""
    try:
        response = requests.post(
            url,
            json=payload,
            headers=_build_headers(key=key),
            timeout=ISOLATION_NOTIFY_TIMEOUT_SECONDS,
        )
        return {
            "url": url,
            "ok": 200 <= response.status_code < 300,
            "http_status": response.status_code,
            "response_body_preview": (response.text or "")[:300],
            "error": None,
        }
    except requests.RequestException as exc:
        return {
            "url": url,
            "ok": False,
            "http_status": None,
            "response_body_preview": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    except Exception as exc:
        logger.exception("Unexpected error posting test notification to %s", url)
        return {
            "url": url,
            "ok": False,
            "http_status": None,
            "response_body_preview": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def send_isolation_test_notification(
    urls: list[str] | None = None,
    key: str | None = None,
    include_payload: bool = False,
) -> dict:
    """Synchronously POST a static dummy isolation payload to client URLs.

    Unlike ``notify_isolation_clients`` (fire-and-forget daemon threads),
    this waits for every HTTP response and returns per-URL outcome. Used
    exclusively by ``POST /api/isolation-test``.

    Parameters
    ----------
    urls
        Override target URLs. Falls back to ``ISOLATION_NOTIFY_CLIENT_URLS``.
    key
        Override shared key. Falls back to ``ISOLATION_NOTIFY_CLIENT_KEY``.
        Pass ``""`` to force-send without a key header.
    include_payload
        When ``True``, the full JSON payload is embedded in the response.
    """
    target_urls = [
        u.strip()
        for u in (urls if urls else ISOLATION_NOTIFY_CLIENT_URLS)
        if u and u.strip()
    ]
    target_urls = list(dict.fromkeys(target_urls))

    if not target_urls:
        return {
            "sent": False,
            "reason": (
                "No client URLs configured. Set ISOLATION_NOTIFY_CLIENT_URLS "
                "in .env, or pass 'urls' in the request body."
            ),
            "results": [],
        }

    try:
        payload = _build_static_test_payload()
    except Exception as exc:
        logger.exception("Test isolation payload build failed")
        return {
            "sent": False,
            "reason": (
                f"Test payload build failed: {type(exc).__name__}: {exc}"
            ),
            "results": [],
        }

    effective_key = key if key is not None else ISOLATION_NOTIFY_CLIENT_KEY
    if not effective_key:
        _warn_missing_key_once()

    results: list[dict] = []
    for url in target_urls:
        logger.info(
            "Sending TEST isolation notification: client=%s "
            "(key_header=%s, key_set=%s)",
            url,
            ISOLATION_NOTIFY_CLIENT_KEY_HEADER,
            bool(effective_key),
        )
        results.append(_post_test_notification(url, payload, key=effective_key))

    response = {
        "sent": True,
        "key_header": ISOLATION_NOTIFY_CLIENT_KEY_HEADER,
        "key_configured": bool(effective_key),
        "payload_summary": {
            "trading_symbol": payload.get("trading_symbol"),
            "standard_execution_mode": payload.get("standard_execution_mode"),
            "is_test": payload.get("is_test"),
            "is_dummy": payload.get("is_dummy"),
            "source": payload.get("source"),
        },
        "results": results,
    }
    if include_payload:
        response["payload"] = payload
    return response