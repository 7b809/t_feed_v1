import os
import json
import upstox_client


# ============================================================
# CONFIG
# ============================================================

# You can provide a single instrument:
INSTRUMENTS = [
    "NSE_FO|73924",
    "NSE_FO|73923",
]

# Or multiple instruments:
# INSTRUMENTS = [
#     "NSE_EQ|INE848E01016",
#     "NSE_EQ|INE002A01018",
#     "NSE_EQ|INE009A01021",
# ]

START_DATE = "2026-09-24"
END_DATE = "2026-09-24"

INTERVAL_UNIT = "minutes"
INTERVAL = "1"

OUTPUT_DIR = "data"


# ============================================================
# FETCH + SAVE
# ============================================================

def fetch_historical_data(instrument_token, start_date, end_date):
    """
    Fetch historical candle data from Upstox History V3 API
    and save it as data/{instrument_token}.json
    """

    print("=" * 70)
    print(f"Instrument : {instrument_token}")
    print(f"From       : {start_date}")
    print(f"To         : {end_date}")
    print("=" * 70)

    api_instance = upstox_client.HistoryV3Api()

    try:
        response = api_instance.get_historical_candle_data1(
            instrument_token,
            INTERVAL_UNIT,
            INTERVAL,
            end_date,
            start_date
        )

        # Convert SDK response to dictionary
        if hasattr(response, "to_dict"):
            data = response.to_dict()
        else:
            data = response

        # Create output directory
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # Keep instrument token as filename
        safe_token = instrument_token.replace("|", "_")
        filename = f"{safe_token}.json"
        output_path = os.path.join(OUTPUT_DIR, filename)

        with open(output_path, "w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                indent=4,
                ensure_ascii=False,
                default=str
            )

        print(f"Saved successfully:")
        print(f"  {output_path}")

        # Print candle count if available
        candles = None

        if isinstance(data, dict):
            candles = (
                data.get("data", {})
                    .get("candles")
            )

        if candles is not None:
            print(f"Candles    : {len(candles)}")

        return data

    except Exception as e:
        print(
            f"Exception when calling "
            f"HistoryV3Api->get_historical_candle_data1 "
            f"for {instrument_token}: {e}"
        )

        return None


# ============================================================
# MAIN
# ============================================================

def main():

    print("\n" + "=" * 70)
    print("UPSTOX HISTORICAL DATA EXPORT")
    print("=" * 70)

    print(f"Instruments : {len(INSTRUMENTS)}")
    print(f"Start Date  : {START_DATE}")
    print(f"End Date    : {END_DATE}")
    print(f"Interval    : {INTERVAL} {INTERVAL_UNIT}")
    print(f"Output      : {OUTPUT_DIR}/")
    print("=" * 70)

    success = 0
    failed = 0

    for instrument in INSTRUMENTS:

        result = fetch_historical_data(
            instrument,
            START_DATE,
            END_DATE
        )

        if result is not None:
            success += 1
        else:
            failed += 1

    print("\n" + "=" * 70)
    print("EXPORT COMPLETED")
    print("=" * 70)
    print(f"Successful : {success}")
    print(f"Failed     : {failed}")
    print(f"Output     : {OUTPUT_DIR}/")
    print("=" * 70)


if __name__ == "__main__":
    main()