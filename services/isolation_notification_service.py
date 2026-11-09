# services/isolation_notification_service.py
"""Fire-and-forget notifications for newly persisted isolation records.

Each configured client receives the complete isolation document as JSON.
The caller only starts daemon worker threads; it never waits for HTTP
responses or client acknowledgements.
"""

from __future__ import annotations

from threading import Thread

import requests

from core.config import (
    ISOLATION_NOTIFY_ENABLED,
    ISOLATION_NOTIFY_CLIENT_URLS,
    ISOLATION_NOTIFY_TIMEOUT_SECONDS,
)
from core.logger import get_logger

logger = get_logger(__file__)


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
        "HTTP responses are not awaited",
        started,
        len(urls),
    )
    return started


def _post_notification(url: str, payload: dict) -> None:
    """POST in a daemon thread; never blocks the isolation scan caller."""
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Accept": "application/json"},
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
