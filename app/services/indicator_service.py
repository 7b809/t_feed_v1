import logging
from typing import Dict, Any, List, Optional

import pandas as pd

logger = logging.getLogger("uvicorn")

# Global in-memory cache
indicator_cache: Dict[str, Dict[str, Any]] = {}


class OptionIndicatorService:
    """
    Calculates EMA indicators and crossover events
    and stores them in memory.
    """

    @staticmethod
    def calculate_emas_and_crossovers(
        candles: List[List[Any]],
        ema_short: int = 9,
        ema_long: int = 21,
    ) -> Dict[str, Any]:
        """
        Calculate EMA series and crossover events.
        """

        if not candles:
            return {
                "total_candles": 0,
                "total_crossovers": 0,
                "crossovers": [],
                "indicator_series": [],
                "last_close": None,
                "last_ema_short": None,
                "last_ema_long": None,
                "last_timestamp": None,
            }

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

        df["close"] = df["close"].astype(float)

        short_col = f"ema_{ema_short}"
        long_col = f"ema_{ema_long}"

        df[short_col] = df["close"].ewm(span=ema_short, adjust=False).mean().round(4)

        df[long_col] = df["close"].ewm(span=ema_long, adjust=False).mean().round(4)

        df["prev_short"] = df[short_col].shift(1)
        df["prev_long"] = df[long_col].shift(1)

        # Bullish Cross
        df["bullish_cross"] = (df["prev_short"] < df["prev_long"]) & (
            df[short_col] >= df[long_col]
        )

        # Bearish Cross
        df["bearish_cross"] = (df["prev_short"] > df["prev_long"]) & (
            df[short_col] <= df[long_col]
        )

        crossovers = []

        for _, row in df[df["bullish_cross"] | df["bearish_cross"]].iterrows():

            crossovers.append(
                {
                    "timestamp": row["timestamp"],
                    "crossover_type": (
                        "Bullish Cross" if row["bullish_cross"] else "Bearish Cross"
                    ),
                    "close": row["close"],
                    short_col: row[short_col],
                    long_col: row[long_col],
                }
            )

        # Latest crossover first
        crossovers.reverse()

        indicator_series = df[
            [
                "timestamp",
                "close",
                short_col,
                long_col,
            ]
        ].to_dict(orient="records")

        latest = indicator_series[-1]

        return {
            "total_candles": len(df),
            "total_crossovers": len(crossovers),
            "crossovers": crossovers,
            "indicator_series": indicator_series,
            # Latest values for live EMA initialization
            "last_timestamp": latest.get("timestamp"),
            "last_close": latest.get("close"),
            "last_ema_short": latest.get(short_col),
            "last_ema_long": latest.get(long_col),
        }

    def process_and_cache_contract_ema(
        self,
        trading_symbol: str,
        candles: List[List[Any]],
        ema_short: int = 9,
        ema_long: int = 21,
    ) -> Optional[Dict[str, Any]]:
        """
        Calculates EMA and stores output in memory cache.
        """

        try:

            results = self.calculate_emas_and_crossovers(
                candles=candles,
                ema_short=ema_short,
                ema_long=ema_long,
            )

            payload = {
                "trading_symbol": trading_symbol,
                "ema_periods": [
                    ema_short,
                    ema_long,
                ],
                **results,
            }

            indicator_cache[trading_symbol] = payload

            logger.info(
                f"Successfully calculated and cached EMA " f"for '{trading_symbol}'"
            )

            return payload

        except Exception as ex:

            logger.error(
                f"Failed to calculate and cache EMA for " f"{trading_symbol}: {ex}"
            )

            return None

    @staticmethod
    def get_cached_ema(
        trading_symbol: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Returns cached EMA payload.
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
