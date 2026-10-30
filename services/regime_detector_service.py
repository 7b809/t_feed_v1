# services/regime_detector_service.py
"""Market Regime Detection & Vectorized Indicator Service (G1 v4.2).

Provides mathematical indicators (Wilder's ATR, ADX/DMI, Bollinger Bandwidth,
EMA, RSI) and market regime classification for both macro indices and option
instruments.

Regimes:
  - COMPRESSION: Volatility squeeze (Bollinger Bandwidth < threshold).
  - TRENDING_BULL: ADX >= threshold AND EMA_fast > EMA_slow AND close > EMA_trend.
  - TRENDING_BEAR: ADX >= threshold AND EMA_fast < EMA_slow AND close < EMA_trend.
  - RANGING: Flat / sideways market with weak directional movement (ADX < threshold).
"""

from __future__ import annotations

import logging
import math
from typing import Any

from core import config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Pure Mathematical Indicator Implementations                                 #
# --------------------------------------------------------------------------- #

def calculate_tr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> list[float]:
    """Calculate True Range series."""
    n = len(highs)
    if n == 0:
        return []
    trs: list[float] = [highs[0] - lows[0]]
    for i in range(1, n):
        h = highs[i]
        l = lows[i]
        prev_c = closes[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return trs


def calculate_wilder_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float]:
    """Calculate Average True Range using Wilder's exponential smoothing."""
    trs = calculate_tr(highs, lows, closes)
    n = len(trs)
    if n == 0:
        return []
    if n < period:
        running = 0.0
        out: list[float] = []
        for i, val in enumerate(trs):
            running += val
            out.append(running / (i + 1))
        return out

    out = [0.0] * n
    first_sum = sum(trs[:period])
    out[period - 1] = first_sum / period

    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period

    running = 0.0
    for i in range(period - 1):
        running += trs[i]
        out[i] = running / (i + 1)

    return out


def calculate_adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> dict[str, list[float]]:
    """Calculate Wilder's Directional Movement Index (ADX, +DI, -DI)."""
    n = len(highs)
    empty_res = {"adx": [0.0] * n, "plus_di": [0.0] * n, "minus_di": [0.0] * n}
    if n < 2:
        return empty_res

    trs = calculate_tr(highs, lows, closes)
    plus_dm: list[float] = [0.0] * n
    minus_dm: list[float] = [0.0] * n

    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        if up_move > down_move and up_move > 0.0:
            plus_dm[i] = up_move
        if down_move > up_move and down_move > 0.0:
            minus_dm[i] = down_move

    if n < period:
        return empty_res

    smooth_tr = [0.0] * n
    smooth_plus = [0.0] * n
    smooth_minus = [0.0] * n

    smooth_tr[period - 1] = sum(trs[:period])
    smooth_plus[period - 1] = sum(plus_dm[:period])
    smooth_minus[period - 1] = sum(minus_dm[:period])

    for i in range(period, n):
        smooth_tr[i] = smooth_tr[i - 1] - (smooth_tr[i - 1] / period) + trs[i]
        smooth_plus[i] = smooth_plus[i - 1] - (smooth_plus[i - 1] / period) + plus_dm[i]
        smooth_minus[i] = smooth_minus[i - 1] - (smooth_minus[i - 1] / period) + minus_dm[i]

    plus_di: list[float] = [0.0] * n
    minus_di: list[float] = [0.0] * n
    dx: list[float] = [0.0] * n

    for i in range(period - 1, n):
        tr_val = smooth_tr[i]
        p_di = (100.0 * smooth_plus[i] / tr_val) if tr_val > 0.0 else 0.0
        m_di = (100.0 * smooth_minus[i] / tr_val) if tr_val > 0.0 else 0.0
        plus_di[i] = p_di
        minus_di[i] = m_di
        di_sum = p_di + m_di
        dx[i] = (100.0 * abs(p_di - m_di) / di_sum) if di_sum > 0.0 else 0.0

    adx: list[float] = [0.0] * n
    adx_start = (2 * period) - 2
    if n > adx_start:
        adx[adx_start] = sum(dx[period - 1 : adx_start + 1]) / period
        for i in range(adx_start + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    elif n >= period:
        adx[-1] = sum(dx[period - 1 :]) / max(1, len(dx[period - 1 :]))

    return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}


def calculate_bollinger_bandwidth(
    closes: list[float],
    period: int = 20,
    std_dev: float = 2.0,
) -> list[float]:
    """Calculate Bollinger Bandwidth: (Upper Band - Lower Band) / Middle Band."""
    n = len(closes)
    out: list[float] = [0.0] * n
    if n == 0:
        return out

    for i in range(n):
        start_idx = max(0, i - period + 1)
        window = closes[start_idx : i + 1]
        w_len = len(window)
        if w_len < 2:
            out[i] = 0.0
            continue
        mean = sum(window) / w_len
        variance = sum((x - mean) ** 2 for x in window) / w_len
        std = math.sqrt(variance)
        upper = mean + (std_dev * std)
        lower = mean - (std_dev * std)
        out[i] = ((upper - lower) / mean) if mean > 0.0 else 0.0

    return out


def calculate_ema(values: list[float], period: int) -> list[float]:
    """Calculate Exponential Moving Average (EMA)."""
    n = len(values)
    if n == 0:
        return []
    if period <= 1:
        return list(values)

    out = [0.0] * n
    alpha = 2.0 / (period + 1.0)
    out[0] = values[0]
    for i in range(1, n):
        out[i] = (values[i] * alpha) + (out[i - 1] * (1.0 - alpha))
    return out


def calculate_rsi(closes: list[float], period: int = 14) -> list[float]:
    """Calculate Relative Strength Index (RSI)."""
    n = len(closes)
    out = [50.0] * n
    if n <= period:
        return out

    gains: list[float] = [0.0] * n
    losses: list[float] = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        if diff > 0.0:
            gains[i] = diff
        else:
            losses[i] = abs(diff)

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period

    if avg_loss == 0.0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0.0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))

    return out


# --------------------------------------------------------------------------- #
# Market Regime Detector Service                                              #
# --------------------------------------------------------------------------- #

class MarketRegimeDetector:
    """G1 v4.2 Market Regime Detector.

    Evaluates trend momentum, directional index (ADX), volatility squeeze
    (Bollinger Bandwidth), and RSI overbought/oversold boundaries.
    """

    def __init__(
        self,
        adx_threshold: float | None = None,
        bandwidth_threshold: float | None = None,
        fast_ema_period: int | None = None,
        slow_ema_period: int | None = None,
        trend_ema_period: int | None = None,
        rsi_period: int | None = None,
    ) -> None:
        self.adx_threshold = (
            adx_threshold
            if adx_threshold is not None
            else float(getattr(config, "STRATEGY_REGIME_MIN_ADX", 22.0))
        )
        self.bandwidth_threshold = (
            bandwidth_threshold
            if bandwidth_threshold is not None
            else float(getattr(config, "STRATEGY_REGIME_BB_BANDWIDTH_MIN", 0.015))
        )
        self.fast_ema_period = (
            fast_ema_period
            if fast_ema_period is not None
            else int(getattr(config, "STRATEGY_REGIME_FAST_EMA", 9))
        )
        self.slow_ema_period = (
            slow_ema_period
            if slow_ema_period is not None
            else int(getattr(config, "STRATEGY_REGIME_SLOW_EMA", 21))
        )
        self.trend_ema_period = (
            trend_ema_period
            if trend_ema_period is not None
            else int(getattr(config, "STRATEGY_REGIME_TREND_EMA", 200))
        )
        self.rsi_period = (
            rsi_period
            if rsi_period is not None
            else int(getattr(config, "STRATEGY_REGIME_RSI_PERIOD", 14))
        )

    def extract_ohlcv(
        self, candles: list[dict[str, Any]] | list[list[Any]],
    ) -> tuple[list[str], list[float], list[float], list[float], list[float], list[float]]:
        """Normalize candle objects into timestamp, O, H, L, C, V series."""
        times: list[str] = []
        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        volumes: list[float] = []

        for c in candles:
            if isinstance(c, dict):
                t = str(c.get("time") or c.get("timestamp") or "")
                o = float(c.get("open") or 0.0)
                h = float(c.get("high") or 0.0)
                l = float(c.get("low") or 0.0)
                close_px = float(c.get("close") or 0.0)
                vol = float(c.get("volume") or 0.0)
            elif isinstance(c, (list, tuple)) and len(c) >= 5:
                t = str(c[0])
                o = float(c[1])
                h = float(c[2])
                l = float(c[3])
                close_px = float(c[4])
                vol = float(c[5]) if len(c) > 5 else 0.0
            else:
                continue

            times.append(t)
            opens.append(o)
            highs.append(h)
            lows.append(l)
            closes.append(close_px)
            volumes.append(vol)

        return times, opens, highs, lows, closes, volumes

    def evaluate(
        self, candles: list[dict[str, Any]] | list[list[Any]],
    ) -> dict[str, Any]:
        """Classify current market regime and return complete indicator state."""
        times, opens, highs, lows, closes, volumes = self.extract_ohlcv(candles)
        n = len(closes)
        if n == 0:
            return {
                "regime": "RANGING",
                "adx": 0.0,
                "plus_di": 0.0,
                "minus_di": 0.0,
                "bb_bandwidth": 0.0,
                "fast_ema": 0.0,
                "slow_ema": 0.0,
                "trend_ema": 0.0,
                "rsi": 50.0,
                "atr": 0.0,
                "close": 0.0,
                "timestamp": None,
                "is_compression": False,
                "is_trending_bull": False,
                "is_trending_bear": False,
                "is_ranging": True,
                "candle_count": 0,
            }

        adx_res = calculate_adx(highs, lows, closes, period=14)
        adx_val = adx_res["adx"][-1]
        plus_di_val = adx_res["plus_di"][-1]
        minus_di_val = adx_res["minus_di"][-1]

        bw_series = calculate_bollinger_bandwidth(closes, period=20, std_dev=2.0)
        bw_val = bw_series[-1]

        fast_ema_s = calculate_ema(closes, period=self.fast_ema_period)
        slow_ema_s = calculate_ema(closes, period=self.slow_ema_period)
        actual_trend_period = min(self.trend_ema_period, max(5, n))
        trend_ema_s = calculate_ema(closes, period=actual_trend_period)

        fast_ema = fast_ema_s[-1]
        slow_ema = slow_ema_s[-1]
        trend_ema = trend_ema_s[-1]

        rsi_series = calculate_rsi(closes, period=self.rsi_period)
        rsi_val = rsi_series[-1]

        atr_series = calculate_wilder_atr(highs, lows, closes, period=14)
        atr_val = atr_series[-1]

        latest_close = closes[-1]
        latest_time = times[-1] if times else None

        # ------------------------------------------------------------- #
        # Classification Logic                                          #
        # ------------------------------------------------------------- #
        regime = "RANGING"

        # 1. Volatility compression / squeeze check
        if bw_val < self.bandwidth_threshold:
            regime = "COMPRESSION"
        # 2. Trending regime check
        elif adx_val >= self.adx_threshold:
            if fast_ema > slow_ema and latest_close >= trend_ema:
                regime = "TRENDING_BULL"
            elif fast_ema < slow_ema and latest_close <= trend_ema:
                regime = "TRENDING_BEAR"
            else:
                regime = "RANGING"
        else:
            regime = "RANGING"

        return {
            "regime": regime,
            "adx": round(adx_val, 4),
            "plus_di": round(plus_di_val, 4),
            "minus_di": round(minus_di_val, 4),
            "bb_bandwidth": round(bw_val, 6),
            "fast_ema": round(fast_ema, 4),
            "slow_ema": round(slow_ema, 4),
            "trend_ema": round(trend_ema, 4),
            "rsi": round(rsi_val, 2),
            "atr": round(atr_val, 4),
            "close": round(latest_close, 4),
            "timestamp": latest_time,
            "is_compression": (regime == "COMPRESSION"),
            "is_trending_bull": (regime == "TRENDING_BULL"),
            "is_trending_bear": (regime == "TRENDING_BEAR"),
            "is_ranging": (regime == "RANGING"),
            "candle_count": n,
            "adx_threshold": self.adx_threshold,
            "bandwidth_threshold": self.bandwidth_threshold,
        }

    def evaluate_market_regime(
        self, underlying: str = "NSE_INDEX|Nifty 50",
    ) -> dict[str, Any]:
        """Fetch live intraday candles for an underlying and evaluate its regime."""
        try:
            from services.live_sr_calc_service import live_sr_calc_service
            candles = live_sr_calc_service.fetch_intraday_candles_for_entry(underlying)
        except Exception as exc:
            logger.warning("[regime-detector] could not fetch candles for %s: %s", underlying, exc)
            candles = []

        return self.evaluate(candles)

    def calculate_instrument_atr(
        self, instrument_key: str, period: int = 14,
    ) -> float | None:
        """Fetch candles for an instrument and calculate current Wilder's ATR."""
        try:
            from services.live_sr_calc_service import live_sr_calc_service
            candles = live_sr_calc_service.fetch_intraday_candles_for_entry(instrument_key)
            if not candles:
                return None
            times, opens, highs, lows, closes, volumes = self.extract_ohlcv(candles)
            if not highs:
                return None
            atr_s = calculate_wilder_atr(highs, lows, closes, period=period)
            return round(atr_s[-1], 4) if atr_s else None
        except Exception as exc:
            logger.warning("[regime-detector] ATR calculation failed for %s: %s", instrument_key, exc)
            return None


# Global singleton instance
regime_detector_service = MarketRegimeDetector()
