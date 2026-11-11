# core/token_service.py
"""Fetch and validate the Upstox access token from MongoDB.

Logging rationale
-----------------
``fetch_access_token`` is called on **every** isolation scan tick —
every 30 s while the market is open — by
``jobs/isolation_scan.py::run_isolation_scan``. The original code
emitted ``Access token fetched (updated_at=...)`` at **INFO** level on
every one of those calls, producing ~120 lines per hour of identical
output (the ``updated_at`` value never changes once the upstream app
has rotated the token for the day). That noise buried the actually
useful log lines in ``logs/local_project.log``.

The log line in ``fetch_access_token`` has been downgraded to DEBUG:

  * The real "token fetched" **milestone** — one per day — is already
    emitted by ``jobs/daily_refresh.run_daily_refresh`` via
    ``tasks["Token fetched"]`` in its ``Daily refresh completed:``
    summary. Nothing useful is lost.
  * ``logging.getLogger(...).setLevel(logging.DEBUG)`` is the project
    default (see ``core/logger.py::get_logger``), so the DEBUG line is
    still written to the file handlers. It just stops flooding the
    **console** and the **combined log** with repeated identical lines.
    Operators who need it can still grep ``logs_text/token_service.txt``.

No behavioural change to callers. The function still:

  * reads from ``app_tokens._id = "upstox_access_token"``,
  * raises ``RuntimeError`` if the document or ``access_token`` field
    is missing,
  * returns the raw token string on success.

``validate_access_token`` is unchanged — it is called at most once per
day by ``run_daily_refresh``, so its INFO output stays at the level
operators expect.
"""

import upstox_client
from upstox_client.rest import ApiException

from core.config import UPSTOX_API_VERSION, APP_TOKENS_COLLECTION
from core.mongo_client import get_mongo_db
from core.logger import get_logger

logger = get_logger(__file__)

TOKEN_DOC_ID = "upstox_access_token"


def fetch_access_token() -> str:
    """Fetch the access token document from MongoDB.

    Called by every isolation scan tick (30 s cadence while the market
    is open) as well as by ``run_daily_refresh``. Logs at **DEBUG**;
    see module docstring for the rationale.
    """
    db = get_mongo_db()
    doc = db[APP_TOKENS_COLLECTION].find_one({"_id": TOKEN_DOC_ID})
    if not doc:
        raise RuntimeError(f"Token document '{TOKEN_DOC_ID}' not found.")
    token = doc.get("access_token")
    if not token:
        raise RuntimeError("access_token field is missing.")
    logger.debug("Access token fetched (updated_at=%s)", doc.get("updated_at"))
    return token


def validate_access_token(token: str) -> bool:
    """Validate the token by calling Upstox ``get_profile``.

    Only the pass/fail outcome is logged. The profile payload returned by
    Upstox (user id, name, broker, email, …) is deliberately **not**
    logged to avoid leaking account details into log files.
    """
    configuration = upstox_client.Configuration()
    configuration.access_token = token
    api_client = upstox_client.ApiClient(configuration)
    user_api = upstox_client.UserApi(api_client)
    try:
        # Call is made; response is intentionally discarded (not logged).
        user_api.get_profile(UPSTOX_API_VERSION)
        logger.info("Token valid.")
        return True
    except ApiException as exc:
        logger.error("Token validation failed: %s", exc)
        return False