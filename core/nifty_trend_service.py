# core/nifty_trend_service.py
"""Decide NIFTY trend (RISING / FALLING / NEUTRAL) from intraday candles.

Uses EMA(N) computed from NIFTY 1-minute candles + slope lookback.
Fallback chain:
    1. If EMA slope (over NIFTY_TREND_SLOPE_LOOKBACK bars) is positive AND
       last close >= EMA  → RISING
    2. If EMA slope negative AND last close <= EMA → FALLING
    3. If EMA unavailable / insufficient data → compare last close to
       day-open → RISING or FALLING
    4. Otherwise → NEUTRAL (bias falls back to configured default)
"""

from __future__ import annotations

from typing import Optional

from core.candle_fetcher import CandleFetcher, CandleFetchError
from core.config import (
    NIFTY_TREND_MA_PERIOD,
    NIFTY_TREND_SLOPE_LOOKBACK,
    NIFTY_TREND_NEUTRAL_FALLBACK,
)
from core.logger import get_logger

logger = get_logger(__file__)

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"


class NiftyTrendService:
    def __init__(self, access_token: str):
        self.fetcher = CandleFetcher(access_token)

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def get_trend(
        self,
        current_ltp: float,
        candles: Optional[list[dict]] = None,
    ) -> dict:
        """Return a dict with trend + supporting evidence + chosen bias.

        {
          "trend": "RISING" | "FALLING" | "NEUTRAL",
          "bias": "CE" | "PE",
          "ema": float | None,
          "ema_slope": float | None,
          "last_close": float | None,
          "day_open": float | None,
          "reason": str,
        }
        """
        if candles is None:
            try:
                candles = self.fetcher.fetch_intraday_candles(NIFTY_INDEX_KEY)
            except CandleFetchError:
                logger.warning(
                    "NIFTY candles unavailable; using NEUTRAL trend"
                )
                return self._neutral("candles unavailable")

        if not candles:
            return self._neutral("empty candle list")

        closes = [c["close"] for c in candles]
        last_close = closes[-1]
        day_open = candles[0]["open"]

        ema_series = _ema(closes, NIFTY_TREND_MA_PERIOD)
        ema_val = ema_series[-1] if ema_series else None
        ema_slope = None
        if len(ema_series) >= NIFTY_TREND_SLOPE_LOOKBACK + 1:
            ema_slope = (
                ema_series[-1]
                - ema_series[-(NIFTY_TREND_SLOPE_LOOKBACK + 1)]
            )

        result = self._classify(
            ltp=current_ltp,
            last_close=last_close,
            day_open=day_open,
            ema=ema_val,
            ema_slope=ema_slope,
        )
        logger.info(
            "NIFTY trend=%s (bias=%s, ema=%s, slope=%s, "
            "last_close=%.2f, day_open=%.2f, reason=%s)",
            result["trend"], result["bias"],
            _fmt(ema_val), _fmt(ema_slope),
            last_close, day_open, result["reason"],
        )
        return result

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _classify(
        self,
        *,
        ltp: float,
        last_close: float,
        day_open: float,
        ema: Optional[float],
        ema_slope: Optional[float],
    ) -> dict:
        if ema is not None and ema_slope is not None:
            if ema_slope > 0 and last_close >= ema:
                return {
                    "trend": "RISING",
                    "bias": "CE",
                    "ema": ema,
                    "ema_slope": ema_slope,
                    "last_close": last_close,
                    "day_open": day_open,
                    "reason": (
                        f"EMA({NIFTY_TREND_MA_PERIOD}) slope over "
                        f"{NIFTY_TREND_SLOPE_LOOKBACK} bars is positive "
                        f"({ema_slope:.4f}) and last_close >= EMA"
                    ),
                }
            if ema_slope < 0 and last_close <= ema:
                return {
                    "trend": "FALLING",
                    "bias": "PE",
                    "ema": ema,
                    "ema_slope": ema_slope,
                    "last_close": last_close,
                    "day_open": day_open,
                    "reason": (
                        f"EMA({NIFTY_TREND_MA_PERIOD}) slope is negative "
                        f"({ema_slope:.4f}) and last_close <= EMA"
                    ),
                }

        # Fallback: intraday bias from last close vs day open
        if last_close > day_open:
            return {
                "trend": "RISING",
                "bias": "CE",
                "ema": ema,
                "ema_slope": ema_slope,
                "last_close": last_close,
                "day_open": day_open,
                "reason": "EMA slope inconclusive; last_close > day_open",
            }
        if last_close < day_open:
            return {
                "trend": "FALLING",
                "bias": "PE",
                "ema": ema,
                "ema_slope": ema_slope,
                "last_close": last_close,
                "day_open": day_open,
                "reason": "EMA slope inconclusive; last_close < day_open",
            }

        return self._neutral(
            "last_close == day_open and EMA inconclusive",
            ema=ema, ema_slope=ema_slope,
            last_close=last_close, day_open=day_open,
        )

    def _neutral(
        self,
        reason: str,
        *,
        ema: Optional[float] = None,
        ema_slope: Optional[float] = None,
        last_close: Optional[float] = None,
        day_open: Optional[float] = None,
    ) -> dict:
        return {
            "trend": "NEUTRAL",
            "bias": NIFTY_TREND_NEUTRAL_FALLBACK,
            "ema": ema,
            "ema_slope": ema_slope,
            "last_close": last_close,
            "day_open": day_open,
            "reason": f"NEUTRAL fallback bias={NIFTY_TREND_NEUTRAL_FALLBACK}: {reason}",
        }


def _ema(values: list[float], period: int) -> list[float]:
    """Return an EMA series (seeded on first value)."""
    if period < 1 or not values:
        return []
    k = 2.0 / (period + 1)
    out: list[float] = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _fmt(x: Optional[float]) -> str:
    return f"{x:.4f}" if isinstance(x, (int, float)) else "None"