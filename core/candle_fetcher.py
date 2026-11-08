# core/candle_fetcher.py
"""Fetch intraday 1-minute candles via HistoryV3Api.

Wraps `upstox_client.HistoryV3Api.get_intra_day_candle_data(
    instrument_key, unit, interval)`.

The SDK returns:

    {
      "status": "success",
      "data": {
        "candles": [
          ["2026-11-08T09:15:00+05:30", o, h, l, c, v, oi],
          ...
        ]
      }
    }

This module normalises that into:

    [{"timestamp": iso_str, "open": o, "high": h, "low": l, "close": c,
      "volume": v, "open_interest": oi}, ...]

Candles are always returned sorted ascending by timestamp.
"""

from __future__ import annotations

import upstox_client

from core.config import OR_FETCH_UNIT, OR_FETCH_INTERVAL
from core.logger import get_logger

logger = get_logger(__file__)


class CandleFetchError(RuntimeError):
    """Raised when the candles endpoint fails or returns no data."""


class CandleFetcher:
    def __init__(self, access_token: str):
        if not access_token:
            raise ValueError("access_token is required")
        self.access_token = access_token
        config = upstox_client.Configuration()
        config.access_token = access_token
        self.api = upstox_client.HistoryV3Api(
            upstox_client.ApiClient(config)
        )

    def fetch_intraday_candles(
        self,
        instrument_key: str,
        unit: str | None = None,
        interval: str | None = None,
    ) -> list[dict]:
        unit = unit or OR_FETCH_UNIT
        interval = interval or OR_FETCH_INTERVAL

        try:
            response = self.api.get_intra_day_candle_data(
                instrument_key, unit, interval
            )
        except Exception as exc:
            raise CandleFetchError(
                f"HistoryV3Api error for {instrument_key}: {exc}"
            ) from exc

        candles_raw = (
            (response or {}).get("data", {}).get("candles", [])
            if isinstance(response, dict)
            else getattr(getattr(response, "data", None), "candles", [])
        )
        if not candles_raw:
            raise CandleFetchError(
                f"No candles returned for {instrument_key}"
            )

        normalised: list[dict] = []
        for row in candles_raw:
            try:
                if isinstance(row, (list, tuple)) and len(row) >= 5:
                    ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
                    v = row[5] if len(row) > 5 else 0
                    oi = row[6] if len(row) > 6 else 0
                    normalised.append(
                        {
                            "timestamp": _iso(ts),
                            "open": float(o),
                            "high": float(h),
                            "low": float(l),
                            "close": float(c),
                            "volume": float(v or 0),
                            "open_interest": float(oi or 0),
                        }
                    )
                elif isinstance(row, dict):
                    normalised.append(
                        {
                            "timestamp": _iso(
                                row.get("timestamp")
                                or row.get("ts")
                                or row.get("time")
                            ),
                            "open": float(row.get("open", 0)),
                            "high": float(row.get("high", 0)),
                            "low": float(row.get("low", 0)),
                            "close": float(row.get("close", 0)),
                            "volume": float(row.get("volume", 0) or 0),
                            "open_interest": float(
                                row.get("open_interest", 0) or 0
                            ),
                        }
                    )
            except (TypeError, ValueError, IndexError):
                logger.warning(
                    "Skipping malformed candle row for %s: %r",
                    instrument_key, row,
                )

        if not normalised:
            raise CandleFetchError(
                f"Candles could not be normalised for {instrument_key}"
            )

        normalised.sort(key=lambda c: c["timestamp"])
        logger.info(
            "Fetched %d intraday candles for %s (%s/%s)",
            len(normalised), instrument_key, unit, interval,
        )
        return normalised


def _iso(ts) -> str:
    """Normalise a candle timestamp to an ISO string (best effort)."""
    if isinstance(ts, str):
        return ts
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)