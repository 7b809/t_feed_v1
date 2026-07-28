import json
import logging
import time as time_module
from datetime import datetime, time, timedelta
from pathlib import Path

import upstox_client
from upstox_client.rest import ApiException

logger = logging.getLogger("uvicorn")


class HistoryService:
    TARGET_TRADING_DAYS = 7
    BATCH_DAYS = 7  # Fetch 7 calendar days per API batch request
    MARKET_OPEN = time(9, 15)
    MARKET_CLOSE = time(15, 30)

    def __init__(self):
        self.history_v3 = upstox_client.HistoryV3Api()
        self.history = upstox_client.HistoryApi()

        # In-memory cache
        self.candles = []
        self.ema_data = []

    # --------------------------------------------------------
    # EMA Calculation & Helpers
    # --------------------------------------------------------

    @staticmethod
    def calculate_ema(values, period):
        multiplier = 2 / (period + 1)

        ema = []
        current = None

        for price in values:
            if current is None:
                current = price
            else:
                current = ((price - current) * multiplier) + current

            ema.append(round(current, 6))

        return ema

    @staticmethod
    def format_to_readable_ist(timestamp_str):
        """
        Converts '2026-07-27T09:15:00+05:30' -> '27-Jul-2026 09:15:00 AM IST'
        """
        try:
            dt = datetime.fromisoformat(timestamp_str)
            return dt.strftime("%d-%b-%Y %I:%M:%S %p IST")
        except Exception:
            return timestamp_str

    @staticmethod
    def merge_and_deduplicate_candles(historical_candles, intraday_candles):
        """
        Merges historical and intraday candles, deduplicating overlapping timestamps
        and maintaining strict chronological order (oldest to newest).
        """
        combined = historical_candles + intraday_candles
        seen_timestamps = set()
        unique_candles = []

        for candle in combined:
            if not candle or len(candle) == 0:
                continue
            ts = candle[0]
            if ts not in seen_timestamps:
                seen_timestamps.add(ts)
                unique_candles.append(candle)

        # Sort chronologically based on ISO timestamp string
        unique_candles.sort(key=lambda c: c[0])
        return unique_candles

    # --------------------------------------------------------
    # History (Batch Range Query)
    # --------------------------------------------------------

    def get_last_7_trading_days_history(
        self,
        instrument_key,
        unit="minutes",
        interval="1",
    ):
        """
        Fetches historical candles in 7-day date range batches.
        If holidays/weekends result in fewer trading days, it continues fetching previous 7-day
        batches until TARGET_TRADING_DAYS (7) valid trading days are collected.
        """
        all_candles = []
        valid_trading_days = 0
        end_date = datetime.now().date()

        logger.info(
            f"Fetching historical candles for '{instrument_key}' (Target trading days: {self.TARGET_TRADING_DAYS})"
        )

        # Loop until 7 distinct trading days with candles are collected
        while valid_trading_days < self.TARGET_TRADING_DAYS:
            start_date = end_date - timedelta(days=self.BATCH_DAYS - 1)

            to_date_str = end_date.strftime("%Y-%m-%d")
            from_date_str = start_date.strftime("%Y-%m-%d")

            logger.info(
                f"Historical API Request: key='{instrument_key}', from={from_date_str}, to={to_date_str}"
            )

            try:
                response = self.history_v3.get_historical_candle_data1(
                    instrument_key,
                    unit,
                    interval,
                    to_date_str,
                    from_date_str,
                )

                # Log full API Response status & details
                logger.info(
                    f"Historical API Response Received | Key='{instrument_key}' | Status='{getattr(response, 'status', 'SUCCESS')}'"
                )

                batch_candles = []
                if response and response.data and response.data.candles:
                    batch_candles = response.data.candles

                count = len(batch_candles)
                logger.info(
                    f"Historical Candle Count: {count} candles received for range {from_date_str} to {to_date_str}"
                )

                if batch_candles:
                    # Upstox returns newest first; reverse to get chronological order (oldest -> newest)
                    batch_candles.reverse()

                    # Extract unique trading dates from candles timestamp
                    unique_dates_in_batch = set(
                        c[0].split("T")[0] for c in batch_candles if c and len(c) > 0
                    )

                    new_trading_days = len(unique_dates_in_batch)
                    valid_trading_days += new_trading_days

                    logger.info(
                        f"Found {new_trading_days} new trading days in batch. Total valid trading days collected: {valid_trading_days}/{self.TARGET_TRADING_DAYS}"
                    )

                    # Prepend batch to maintain proper chronological timeline
                    all_candles = batch_candles + all_candles
                else:
                    logger.warning(
                        f"No candles returned for range {from_date_str} to {to_date_str}"
                    )

            except ApiException as e:
                logger.error(
                    f"Upstox Historical API Exception for {instrument_key} [{from_date_str} to {to_date_str}]: {e}"
                )

            # Move batch window back by 7 days for next iteration
            end_date = start_date - timedelta(days=1)

            # Safety fallback to prevent infinite loops (max 30 calendar days search window)
            if (datetime.now().date() - end_date).days > 30:
                logger.warning(
                    f"Reached max search window (30 calendar days). Stopping historical batch fetching."
                )
                break

        logger.info(
            f"Completed historical fetch for '{instrument_key}'. Total historical candles: {len(all_candles)}"
        )
        return all_candles

    # --------------------------------------------------------
    # Intraday
    # --------------------------------------------------------

    def get_intraday_candles(
        self,
        instrument_key,
        interval="1minute",
        api_version="2.0",
    ):
        logger.info(
            f"Intraday API Request: key='{instrument_key}', interval='{interval}', api_version='{api_version}'"
        )

        # Throttle request rate to avoid Upstox 429 Too Many Requests errors
        time_module.sleep(0.25)

        try:
            response = self.history.get_intra_day_candle_data(
                instrument_key,
                interval,
                api_version,
            )

            # Log full API Response status & details
            logger.info(
                f"Intraday API Response Received | Key='{instrument_key}' | Status='{getattr(response, 'status', 'SUCCESS')}'"
            )

            if response and response.data and response.data.candles:
                intraday_candles = response.data.candles
                count = len(intraday_candles)
                logger.info(
                    f"Intraday Candle Count: received {count} intraday candles for '{instrument_key}'"
                )

                # Upstox returns intraday candles newest-first; reverse to match historical ordering
                intraday_candles.reverse()
                return intraday_candles
            else:
                logger.warning(
                    f"Intraday API Response: no candles returned for '{instrument_key}'"
                )

        except ApiException as e:
            logger.error(f"Upstox Intraday API Exception for {instrument_key}: {e}")

        return []

    # --------------------------------------------------------
    # Save JSON
    # --------------------------------------------------------

    @staticmethod
    def save_json(path, data):
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Saved JSON data to file: {path}")

    # --------------------------------------------------------
    # Main function
    # --------------------------------------------------------

    def load_candles(
        self,
        instrument_key,
        include_intraday=True,
        save_files=False,
        output_dir="data",
    ):
        """
        Main function.

        Before 9:15:
            History only.

        Between 9:15 and 15:30:
            History + today's intraday (Merged & Deduplicated).

        After 15:30:
            History only.
        """
        logger.info(
            f"Starting load_candles for '{instrument_key}' (include_intraday={include_intraday}, save_files={save_files})"
        )

        historical_candles = self.get_last_7_trading_days_history(instrument_key)
        intraday_candles = []
        intraday_added = False
        now = datetime.now().time()

        if include_intraday and self.MARKET_OPEN <= now <= self.MARKET_CLOSE:
            logger.info(
                f"Market is OPEN (Current time: {now.strftime('%H:%M:%S')}). Fetching intraday candles."
            )
            intraday_candles = self.get_intraday_candles(instrument_key)

            if intraday_candles:
                intraday_added = True
                logger.info(
                    f"Successfully retrieved {len(intraday_candles)} intraday candles."
                )
        else:
            logger.info(
                f"Market is CLOSED or intraday disabled (Current time: {now.strftime('%H:%M:%S')}). Skipping intraday fetch."
            )

        # Merge historical + intraday candles with deduplication & chronological sorting
        candles = self.merge_and_deduplicate_candles(
            historical_candles, intraday_candles
        )

        if not candles:
            logger.warning(f"No candle data available for '{instrument_key}'.")
            return {
                "history_days": self.TARGET_TRADING_DAYS,
                "intraday_added": intraday_added,
                "total_candles": 0,
                "total_crossovers": 0,
                "ema": [],
            }

        closes = [float(c[4]) for c in candles]

        logger.info(
            f"Calculating EMA 9 and EMA 21 across {len(closes)} merged close prices..."
        )
        ema9 = self.calculate_ema(closes, 9)
        ema21 = self.calculate_ema(closes, 21)

        crossover_events = []

        # Pine Script cross(short, long) equivalent loop
        for i in range(1, len(candles)):
            prev_e9, curr_e9 = ema9[i - 1], ema9[i]
            prev_e21, curr_e21 = ema21[i - 1], ema21[i]

            # Condition 1: Bullish Crossover (EMA 9 crosses ABOVE EMA 21)
            bullish_cross = (prev_e9 < prev_e21) and (curr_e9 >= curr_e21)

            # Condition 2: Bearish Crossover (EMA 9 crosses BELOW EMA 21)
            bearish_cross = (prev_e9 > prev_e21) and (curr_e9 <= curr_e21)

            if bullish_cross or bearish_cross:
                candle = candles[i]
                formatted_time = self.format_to_readable_ist(candle[0])
                crossover_direction = (
                    "Bullish Cross" if bullish_cross else "Bearish Cross"
                )

                crossover_events.append(
                    {
                        "timestamp": formatted_time,
                        "raw_timestamp": candle[0],
                        "crossover_type": crossover_direction,
                        "open": candle[1],
                        "high": candle[2],
                        "low": candle[3],
                        "close": candle[4],
                        "volume": candle[5] if len(candle) > 5 else None,
                        "ema9": curr_e9,
                        "ema21": curr_e21,
                        "cross_price": curr_e9,
                    }
                )

        logger.info(
            f"EMA calculation completed. Found {len(crossover_events)} crossover events out of {len(candles)} total candles."
        )

        # Reverse crossover events so the latest crossover appears first
        crossover_events.reverse()

        self.candles = candles
        self.ema_data = crossover_events

        if save_files:
            self.save_json(
                f"{output_dir}/candles.json",
                {
                    "instrument_key": instrument_key,
                    "total_candles": len(candles),
                    "candles": candles,
                },
            )

            self.save_json(
                f"{output_dir}/ema_crossovers.json",
                {
                    "instrument_key": instrument_key,
                    "total_crossovers": len(crossover_events),
                    "crossovers": crossover_events,
                },
            )

        logger.info(f"Successfully processed request for '{instrument_key}'.")

        return {
            "history_days": self.TARGET_TRADING_DAYS,
            "intraday_added": intraday_added,
            "total_candles": len(candles),
            "total_crossovers": len(crossover_events),
            "ema": crossover_events,
        }
