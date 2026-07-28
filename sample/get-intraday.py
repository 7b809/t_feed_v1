from datetime import datetime, timedelta, time
from pathlib import Path
import json

import upstox_client
from upstox_client.rest import ApiException


class HistoryService:
    HISTORY_DAYS = 7
    MARKET_OPEN = time(9, 15)
    MARKET_CLOSE = time(15, 30)

    def __init__(self):
        self.history_v3 = upstox_client.HistoryV3Api()
        self.history = upstox_client.HistoryApi()

        # In-memory cache
        self.candles = []
        self.ema_data = []

    # --------------------------------------------------------
    # EMA
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

    # --------------------------------------------------------
    # History
    # --------------------------------------------------------

    def get_last_7_trading_days_history(
        self,
        instrument_key,
        unit="minutes",
        interval="1",
    ):

        candles = []

        valid_days = 0
        current = datetime.now().date()

        while valid_days < self.HISTORY_DAYS:

            if current.weekday() >= 5:
                current -= timedelta(days=1)
                continue

            date_str = current.strftime("%Y-%m-%d")

            try:

                response = self.history_v3.get_historical_candle_data1(
                    instrument_key,
                    unit,
                    interval,
                    date_str,
                    date_str,
                )

                day_candles = []

                if (
                    response
                    and response.data
                    and response.data.candles
                ):
                    day_candles = response.data.candles

                if day_candles:

                    day_candles.reverse()

                    candles.extend(day_candles)

                    valid_days += 1

                    print(f"✓ {date_str} -> {len(day_candles)} candles")

                else:

                    print(f"✗ {date_str} -> No candles")

            except ApiException as e:
                print(e)

            current -= timedelta(days=1)

        return candles

    # --------------------------------------------------------
    # Intraday
    # --------------------------------------------------------

    def get_intraday_candles(
        self,
        instrument_key,
        interval="1minute",
        api_version="2.0",
    ):

        try:

            response = self.history.get_intra_day_candle_data(
                instrument_key,
                interval,
                api_version,
            )

            if (
                response
                and response.data
                and response.data.candles
            ):
                return response.data.candles

        except ApiException as e:
            print(e)

        return []

    # --------------------------------------------------------
    # Save JSON
    # --------------------------------------------------------

    @staticmethod
    def save_json(path, data):

        Path(path).parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

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
            History + today's intraday.

        After 15:30:
            History only.
        """

        print("=" * 60)
        print("Loading history...")

        candles = self.get_last_7_trading_days_history(
            instrument_key
        )

        now = datetime.now().time()

        intraday_added = False

        if (
            include_intraday
            and self.MARKET_OPEN <= now <= self.MARKET_CLOSE
        ):

            print("Loading intraday...")

            intraday = self.get_intraday_candles(
                instrument_key
            )

            if intraday:

                # API usually returns oldest->latest already.
                candles.extend(intraday)

                intraday_added = True

                print(
                    f"✓ Intraday -> {len(intraday)} candles"
                )

            else:

                print("✗ No intraday candles")

        print("=" * 60)

        closes = [float(c[4]) for c in candles]

        ema9 = self.calculate_ema(closes, 9)
        ema21 = self.calculate_ema(closes, 21)

        ema_rows = []

        for candle, e9, e21 in zip(
            candles,
            ema9,
            ema21,
        ):

            ema_rows.append(
                {
                    "timestamp": candle[0],
                    "open": candle[1],
                    "high": candle[2],
                    "low": candle[3],
                    "close": candle[4],
                    "volume": candle[5] if len(candle) > 5 else None,
                    "ema9": e9,
                    "ema21": e21,
                }
            )

        self.candles = candles
        self.ema_data = ema_rows

        if save_files:

            self.save_json(
                f"{output_dir}/candles.json",
                candles,
            )

            self.save_json(
                f"{output_dir}/ema.json",
                ema_rows,
            )

            print(f"Saved files to {output_dir}")

        return {
            "history_days": self.HISTORY_DAYS,
            "intraday_added": intraday_added,
            "total_candles": len(candles),
            "candles": candles,
            "ema": ema_rows,
        }


# --------------------------------------------------------
# Example
# --------------------------------------------------------

service = HistoryService()

# result = service.load_candles(
#     instrument_key="NSE_EQ|INE848E01016",
#     include_intraday=True,
#     save_files=True,
# )

# print("\nSummary")
# print("-" * 40)
# print("Total candles :", result["total_candles"])
# print("Intraday added:", result["intraday_added"])
# print("EMA rows      :", len(result["ema"]))