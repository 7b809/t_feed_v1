import logging
from typing import Dict, Any, List, Optional
import pandas as pd

logger = logging.getLogger("uvicorn")

# Global in-memory cache for computed option indicators
# Structure: { trading_symbol_or_instrument_key: { "trading_symbol": ..., "ema_periods": [9, 21], ... } }
indicator_cache: Dict[str, Dict[str, Any]] = {}


class OptionIndicatorService:
    """
    Service responsible for calculating Technical Indicators (EMA 9, EMA 21, etc.)
    and EMA Crossovers for cached candle datasets and storing them in project memory cache.
    """

    @staticmethod
    def calculate_emas_and_crossovers(
        candles: List[List[Any]],
        ema_short: int = 9,
        ema_long: int = 21,
    ) -> Dict[str, Any]:
        """
        Processes candle data into a Pandas DataFrame, computes EMAs,
        identifies crossover events, and returns structured payload.
        """
        if not candles or len(candles) == 0:
            return {"total_candles": 0, "total_crossovers": 0, "crossovers": [], "indicator_series": []}

        # Upstox candle standard array format: [timestamp, open, high, low, close, volume, open_interest]
        df = pd.DataFrame(
            candles,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "open_interest",
            ][: len(candles[0])],
        )

        # Ensure close price is float
        df["close"] = df["close"].astype(float)

        # Calculate Exponential Moving Averages (EMA)
        df[f"ema_{ema_short}"] = (
            df["close"].ewm(span=ema_short, adjust=False).mean().round(4)
        )
        df[f"ema_{ema_long}"] = (
            df["close"].ewm(span=ema_long, adjust=False).mean().round(4)
        )

        # Identify Crossovers
        df["prev_short"] = df[f"ema_{ema_short}"].shift(1)
        df["prev_long"] = df[f"ema_{ema_long}"].shift(1)

        # Bullish Cross: Short EMA crosses ABOVE Long EMA
        df["bullish_cross"] = (df["prev_short"] < df["prev_long"]) & (
            df[f"ema_{ema_short}"] >= df[f"ema_{ema_long}"]
        )

        # Bearish Cross: Short EMA crosses BELOW Long EMA
        df["bearish_cross"] = (df["prev_short"] > df["prev_long"]) & (
            df[f"ema_{ema_short}"] <= df[f"ema_{ema_long}"]
        )

        crossovers = []
        for idx, row in df[df["bullish_cross"] | df["bearish_cross"]].iterrows():
            crossovers.append(
                {
                    "timestamp": row["timestamp"],
                    "crossover_type": (
                        "Bullish Cross" if row["bullish_cross"] else "Bearish Cross"
                    ),
                    "close": row["close"],
                    f"ema_{ema_short}": row[f"ema_{ema_short}"],
                    f"ema_{ema_long}": row[f"ema_{ema_long}"],
                }
            )

        # Reverse crossovers to keep latest crossover first
        crossovers.reverse()

        # Format full EMA history
        indicator_series = df[
            ["timestamp", "close", f"ema_{ema_short}", f"ema_{ema_long}"]
        ].to_dict(orient="records")

        return {
            "total_candles": len(df),
            "total_crossovers": len(crossovers),
            "crossovers": crossovers,
            "indicator_series": indicator_series,
        }

    def process_and_cache_contract_ema(
        self,
        trading_symbol: str,
        candles: List[List[Any]],
        ema_short: int = 9,
        ema_long: int = 21,
    ) -> Optional[Dict[str, Any]]:
        """
        Calculates EMA for a single contract candle list and stores the output in project memory cache.
        """
        try:
            results = self.calculate_emas_and_crossovers(
                candles, ema_short=ema_short, ema_long=ema_long
            )

            payload = {
                "trading_symbol": trading_symbol,
                "ema_periods": [ema_short, ema_long],
                **results,
            }

            # Store in project cache memory
            indicator_cache[trading_symbol] = payload

            logger.info(
                f"Successfully calculated and cached EMA data in memory for '{trading_symbol}'"
            )
            return payload

        except Exception as e:
            logger.error(f"Failed to calculate and cache EMA for {trading_symbol}: {e}")
            return None

    @staticmethod
    def get_cached_ema(trading_symbol: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves computed EMA payload for a contract directly from in-memory cache.
        """
        return indicator_cache.get(trading_symbol)

    @staticmethod
    def clear_cache() -> None:
        """
        Clears indicator cache.
        """
        indicator_cache.clear()
        logger.info("Indicator memory cache cleared.")


indicator_service = OptionIndicatorService()