# services/market_holiday_service.py
"""Market holiday service — Upstox Market Holidays & Timings API wrapper.

Responsibilities
----------------
* Ask Upstox whether a given date (or today) is a trading holiday.
* Cache results in-process so repeated calls within a refresh / dashboard
  poll do not hammer Upstox.
* Return a normalised, serialisable payload that is safe to log,
  persist, or return from an admin endpoint.

Design notes
------------
* No DB writes, no strategy side effects — this service only reports.
* Never raises for expected failure modes; the caller always receives
  a summary dict with ``status`` in ``{"holiday", "not_holiday", "error"}``
  and a normalised ``holidays`` list.
* The Upstox SDK does **not** reliably return a plain ``dict`` — it may
  return an ``ApiResponse`` / model object, a JSON string, or a dict
  depending on SDK version and endpoint. All of these are coerced to a
  plain dict before normalisation (see ``_coerce_to_dict``).
* The Upstox SDK response contains ``datetime`` objects (``_date``) and
  millisecond epoch timestamps (``start_time`` / ``end_time``). These
  are converted to plain ISO strings before leaving the service so the
  payload is JSON-serialisable and safe to send over Telegram / HTTP.

Upstox API reference
--------------------
    MarketHolidaysAndTimingsApi.get_holiday(date_str)

    Returns (on success, after coercion):
        {
          "status": "success",
          "data": [
            {
              "_date": datetime(...),
              "closed_exchanges": ["NSE", "NFO", ...],
              "description": "Dussehra",
              "holiday_type": "TRADING_HOLIDAY",
              "open_exchanges": [
                {"exchange": "MCX", "start_time": <ms>, "end_time": <ms>},
                ...
              ],
            },
            ...,
          ],
        }

    An empty ``data`` list means the date is not a holiday.

Public API
----------
    market_holiday_service.get_holiday(date_str) -> dict
    market_holiday_service.is_trading_holiday(date_str) -> bool
    market_holiday_service.get_today() -> dict
    market_holiday_service.clear_cache() -> None
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timezone
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Config defaults (kept local so the service works without extra .env vars)   #
# --------------------------------------------------------------------------- #
# Cache TTL in seconds. A holiday result for a specific date never changes
# once the date is in the past, but the Upstox API can add late-notified
# holidays for the current/future date, so a short TTL is used.
_CACHE_TTL_SECONDS = 3600  # 1 hour


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _today_iso() -> str:
    try:
        return datetime.now().astimezone().date().isoformat()
    except Exception:
        return date.today().isoformat()


def _normalise_date_str(value: Any) -> str:
    """Return a ``YYYY-MM-DD`` string or raise ValueError.

    Accepts ``date`` / ``datetime`` objects, epoch seconds/millis, and
    strings in ``YYYY-MM-DD``, ``YYYY/MM/DD``, or ISO 8601 form.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, (int, float)):
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        dt = datetime.fromtimestamp(n, tz=timezone.utc)
        return dt.date().isoformat()

    text = str(value or "").strip()
    if not text:
        raise ValueError("date is required")

    # Accept the common separators and ISO forms.
    cleaned = text.replace("/", "-").replace(".", "-")
    try:
        return datetime.fromisoformat(cleaned).date().isoformat()
    except ValueError:
        pass

    # Accept a bare ``YYYY-MM-DD`` prefix if the tail is a time fragment.
    head = cleaned.split("T", 1)[0].split(" ", 1)[0]
    try:
        return datetime.fromisoformat(head).date().isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid date: {value!r}") from exc


def _ms_to_iso(value: Any) -> str | None:
    """Convert an epoch-millis or epoch-seconds value to an ISO string."""
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if n > 1e12:
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return str(value)


def _coerce_to_dict(raw: Any) -> dict[str, Any] | None:
    """Best-effort conversion of an Upstox SDK response to a plain dict.

    The SDK has returned, across versions / endpoints:
        * a plain ``dict``,
        * an ``ApiResponse`` wrapper whose ``.data`` holds the dict,
        * a model object exposing ``.to_dict()``,
        * a model object whose ``__dict__`` holds the fields,
        * a JSON string.

    Returns ``None`` when none of these strategies yield a dict.
    """
    if raw is None:
        return None

    # 1. Already a dict.
    if isinstance(raw, dict):
        return raw

    # 2. JSON string.
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    # 3. SDK model object with .to_dict().
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        try:
            coerced = to_dict()
        except Exception:
            coerced = None
        if isinstance(coerced, dict):
            return coerced

    # 4. ApiResponse-style wrapper exposing ``.data`` as a dict.
    data_attr = getattr(raw, "data", None)
    if isinstance(data_attr, dict):
        return data_attr

    # 5. Plain object — pull public fields from __dict__.
    raw_dict = getattr(raw, "__dict__", None)
    if isinstance(raw_dict, dict):
        # Drop private/dunder noise the SDK model may include.
        cleaned = {
            k: v
            for k, v in raw_dict.items()
            if not str(k).startswith("_") or k == "_date"
        }
        # Only accept it if it looks like a holiday payload.
        if cleaned and (
            "data" in cleaned or "status" in cleaned
        ):
            return cleaned

    return None


def _normalise_holiday(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe holiday record."""
    if not isinstance(raw, dict):
        return {}

    raw_date = raw.get("_date") or raw.get("date")
    try:
        iso_date = _normalise_date_str(raw_date)
    except ValueError:
        iso_date = str(raw_date)

    open_exchanges: list[dict[str, Any]] = []
    for item in raw.get("open_exchanges") or []:
        if not isinstance(item, dict):
            continue
        open_exchanges.append(
            {
                "exchange": str(item.get("exchange") or ""),
                "start_time_iso": _ms_to_iso(item.get("start_time")),
                "end_time_iso": _ms_to_iso(item.get("end_time")),
                "start_time_ms": item.get("start_time"),
                "end_time_ms": item.get("end_time"),
            }
        )

    return {
        "date": iso_date,
        "description": str(raw.get("description") or ""),
        "holiday_type": str(raw.get("holiday_type") or ""),
        "closed_exchanges": [
            str(x) for x in (raw.get("closed_exchanges") or [])
        ],
        "open_exchanges": open_exchanges,
    }


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class MarketHolidayService:
    """Wraps ``upstox_client.MarketHolidaysAndTimingsApi``."""

    def __init__(self) -> None:
        # Cache: date_str -> (cached_at_epoch, payload)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def get_holiday(self, date_str: str | None = None) -> dict[str, Any]:
        """Return holiday info for ``date_str`` (defaults to today).

        Never raises. The returned dict always has:

            {
              "status":        "holiday" | "not_holiday" | "error",
              "date":          "YYYY-MM-DD",
              "is_holiday":    bool,
              "holiday_count": int,
              "holidays":      [normalised holiday records],
              "cached":        bool,
              "error":         str | None,
              "fetched_at":    ISO timestamp,
            }
        """
        query_date = date_str or _today_iso()

        try:
            iso_date = _normalise_date_str(query_date)
        except ValueError as exc:
            return self._error_payload(
                query_date=str(query_date),
                error=f"invalid_date: {exc}",
            )

        # ---- cache lookup ------------------------------------------- #
        cached = self._cache_lookup(iso_date)
        if cached is not None:
            payload = dict(cached)
            payload["cached"] = True
            return payload

        # ---- live call ---------------------------------------------- #
        try:
            configuration = upstox_client.Configuration()
            # The Upstox SDK will pick up the access token from config if
            # it was set on a previous Configuration(); the holidays
            # endpoint does not require auth, but the SDK still needs a
            # valid client instance.
            api = upstox_client.MarketHolidaysAndTimingsApi(
                upstox_client.ApiClient(configuration)
            )
            raw = api.get_holiday(iso_date)
        except ApiException as exc:
            status = getattr(exc, "status", None)
            logger.warning(
                "[market-holiday] ApiException for %s (HTTP %s)",
                iso_date,
                status,
            )
            return self._error_payload(
                query_date=iso_date,
                error=f"Upstox ApiException (HTTP {status})",
            )
        except Exception as exc:
            logger.exception(
                "[market-holiday] unexpected error for %s", iso_date
            )
            return self._error_payload(
                query_date=iso_date,
                error=f"{type(exc).__name__}: {exc}",
            )

        # ---- coerce SDK response to a plain dict -------------------- #
        raw_dict = _coerce_to_dict(raw)
        if raw_dict is None:
            logger.warning(
                "[market-holiday] uncoercible Upstox payload for %s "
                "(type=%s)",
                iso_date,
                type(raw).__name__,
            )
            return self._error_payload(
                query_date=iso_date,
                error=(
                    "Upstox returned uncoercible payload "
                    f"(type={type(raw).__name__})"
                ),
            )

        # ---- normalise ---------------------------------------------- #
        items = raw_dict.get("data") or []
        if not isinstance(items, list):
            items = []

        holidays = [
            _normalise_holiday(item)
            for item in items
            if isinstance(item, dict)
        ]

        payload: dict[str, Any] = {
            "status": "holiday" if holidays else "not_holiday",
            "date": iso_date,
            "is_holiday": bool(holidays),
            "holiday_count": len(holidays),
            "holidays": holidays,
            "cached": False,
            "error": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        self._cache_store(iso_date, payload)
        return payload

    def is_trading_holiday(self, date_str: str | None = None) -> bool:
        """Convenience wrapper — True only when the date is a holiday."""
        return bool(self.get_holiday(date_str).get("is_holiday"))

    def get_today(self) -> dict[str, Any]:
        """Return today's holiday payload (convenience wrapper)."""
        return self.get_holiday(_today_iso())

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
        logger.info("[market-holiday] cache cleared")

    def last_status(self) -> dict[str, Any]:
        """Return a light snapshot for diagnostics."""
        with self._lock:
            keys = sorted(self._cache.keys())
        return {
            "cached_dates": keys,
            "cache_size": len(keys),
            "ttl_seconds": _CACHE_TTL_SECONDS,
        }

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _cache_lookup(self, iso_date: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            entry = self._cache.get(iso_date)
            if entry is None:
                return None
            cached_at, payload = entry
            if now - cached_at > _CACHE_TTL_SECONDS:
                self._cache.pop(iso_date, None)
                return None
            return payload

    def _cache_store(self, iso_date: str, payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            self._cache[iso_date] = (now, payload)

    @staticmethod
    def _error_payload(*, query_date: str, error: str) -> dict[str, Any]:
        return {
            "status": "error",
            "date": query_date,
            "is_holiday": False,
            "holiday_count": 0,
            "holidays": [],
            "cached": False,
            "error": error,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }


market_holiday_service = MarketHolidayService()