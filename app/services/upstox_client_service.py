# app/services/upstox_client_service.py
#
# Thin wrapper around upstox_client.HistoryV3Api.
#
#   * intraday_1m(instrument_key)                        -> today's 1-min candles
#   * historical_1m(instrument_key, from_date, to_date)  -> historical 1-min candles
#   * historical_window(instrument_key, days)            -> merged multi-day history
#
# Authentication
# --------------
# No access token is used anywhere in this project. The SDK client is
# constructed with a default Configuration and every request goes out
# without an Authorization header. If Upstox ever requires auth for a
# specific endpoint, the real HTTP error is surfaced via UpstoxError so
# the caller sees exactly what the server rejected.
#
# Thread-safety
# -------------
# _client() uses double-checked locking so concurrent warm-up tasks
# (invoked via asyncio.to_thread) cannot race and build multiple SDK
# clients. The client is constructed exactly once per process.

from __future__ import annotations

import logging
import threading
from datetime import date, timedelta
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)


class UpstoxError(RuntimeError):
    """Raised when the Upstox SDK cannot be constructed or a candle call fails."""


# ---------------------------------------------------------------------------
# Candle normalisation
# ---------------------------------------------------------------------------
def _normalize_candle(row: Any) -> dict | None:
    """
    Convert an Upstox candle row into the project's dict form.

    Upstox row forms seen in the wild:
        [timestamp, open, high, low, close, volume, open_interest]   (list)
        {"timestamp": ..., "open": ..., ...}                          (dict)
    Rows with a non-positive close are non-trading / padding and are dropped.
    """
    try:
        if isinstance(row, dict):
            close = float(row.get("close", 0))
            if close <= 0:
                return None
            return {
                "timestamp": row.get("timestamp"),
                "open": float(row.get("open", close)),
                "high": float(row.get("high", close)),
                "low": float(row.get("low", close)),
                "close": close,
                "volume": float(row.get("volume", 0) or 0),
            }

        # List/tuple form: [ts, o, h, l, c, v, oi]
        close = float(row[4])
        if close <= 0:
            return None
        return {
            "timestamp": row[0],
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": close,
            "volume": float(row[5]) if len(row) > 5 and row[5] is not None else 0.0,
        }
    except (TypeError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class UpstoxService:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self._api = None
        self._client_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Client construction (anonymous — no token, no Authorization header)
    # ------------------------------------------------------------------ #
    def _client(self):
        # Fast path: already constructed.
        if self._api is not None:
            return self._api

        with self._client_lock:
            # Double-check inside the lock: another thread may have won.
            if self._api is not None:
                return self._api

            try:
                import upstox_client  # local import: optional at module import time
            except ImportError as exc:
                raise UpstoxError(
                    "upstox-python-sdk is not installed. "
                    "Run: pip install upstox-python-sdk"
                ) from exc

            try:
                configuration = upstox_client.Configuration()
                api_client = upstox_client.ApiClient(configuration)
                self._api = upstox_client.HistoryV3Api(api_client)
            except Exception as exc:  # noqa: BLE001
                raise UpstoxError(
                    f"Failed to construct Upstox HistoryV3Api: {exc}"
                ) from exc

            logger.info("Upstox client initialized (anonymous, no access token)")
            return self._api

    # ------------------------------------------------------------------ #
    # Intraday
    # ------------------------------------------------------------------ #
    def intraday_1m(self, instrument_key: str) -> list[dict]:
        """Fetch today's 1-minute candles for ``instrument_key``."""
        api = self._client()
        try:
            response = api.get_intra_day_candle_data(instrument_key, "minutes", "1")
        except Exception as exc:  # noqa: BLE001
            raise UpstoxError(
                f"intraday fetch failed for {instrument_key}: {exc}"
            ) from exc
        return self._extract(response)

    # ------------------------------------------------------------------ #
    # Historical (single window)
    # ------------------------------------------------------------------ #
    def historical_1m(
        self, instrument_key: str, from_date: str, to_date: str
    ) -> list[dict]:
        """Fetch 1-minute historical candles for an explicit date range."""
        api = self._client()
        try:
            response = api.get_historical_candle_data1(
                instrument_key, "minutes", "1", to_date, from_date
            )
        except Exception as exc:  # noqa: BLE001
            raise UpstoxError(
                f"historical fetch failed for {instrument_key} "
                f"({from_date}..{to_date}): {exc}"
            ) from exc
        return self._extract(response)

    # ------------------------------------------------------------------ #
    # Historical (multi-window walk-back)
    # ------------------------------------------------------------------ #
    def historical_window(self, instrument_key: str, days: int) -> list[dict]:
        """
        Walk backwards in ~7-day windows until ``days`` calendar days are covered,
        merge non-zero candles, and de-duplicate by timestamp.

        Failures on individual windows are logged and skipped so one bad
        window does not abort warm-up for the whole instrument.
        """
        today = date.today()
        collected: dict[str, dict] = {}
        remaining = max(int(days), 1)
        window_end = today

        while remaining > 0:
            window_start = window_end - timedelta(days=6)
            remaining -= 7

            try:
                rows = self.historical_1m(
                    instrument_key,
                    window_start.isoformat(),
                    window_end.isoformat(),
                )
            except UpstoxError as exc:
                logger.warning(
                    "Historical window skipped for %s (%s..%s): %s",
                    instrument_key,
                    window_start.isoformat(),
                    window_end.isoformat(),
                    exc,
                )
                rows = []

            for candle in rows:
                key = str(candle.get("timestamp"))
                collected[key] = candle

            window_end = window_start - timedelta(days=1)

        candles = sorted(collected.values(), key=lambda c: str(c.get("timestamp")))
        logger.debug(
            "historical_window(%s, %s days) -> %d candles",
            instrument_key,
            days,
            len(candles),
        )
        return candles

    # ------------------------------------------------------------------ #
    # Response unwrapping
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract(response: Any) -> list[dict]:
        """
        Unwrap SDK responses across the shapes different SDK versions return:
        object with ``.data.candles``, dict with ``data.candles``, plain dict
        with ``candles``, or a raw list.
        """
        raw_rows = None

        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict):
                raw_rows = data.get("candles")
            if raw_rows is None:
                raw_rows = response.get("candles")
        else:
            data = getattr(response, "data", None)
            if data is not None:
                raw_rows = getattr(data, "candles", None)
                if raw_rows is None and isinstance(data, dict):
                    raw_rows = data.get("candles")

        if raw_rows is None and isinstance(response, (list, tuple)):
            raw_rows = response

        if not raw_rows:
            return []

        out: list[dict] = []
        for row in raw_rows:
            candle = _normalize_candle(row)
            if candle:
                out.append(candle)
        return out