import json
from pathlib import Path
import upstox_client

# ==================================================================
# CONFIGURATION
# ==================================================================
OUTPUT_DIR = Path("upstox_intraday_candles")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Define your list of instruments and intervals to loop through
INSTRUMENT_LIST = [
    "NSE_FO|47142",
    "NSE_INDEX|Nifty 50",
    "BSE_INDEX|SENSEX",
]

INTERVALS = [
    "1",  "3",  "5"
]  # Intervals in minutes (as strings expected by API)

# Optional: If you need to set up your access token globally or via configuration
# configuration = upstox_client.Configuration()
# configuration.access_token = "YOUR_ACCESS_TOKEN"
# api_instance = upstox_client.HistoryV3Api(upstox_client.ApiClient(configuration))


def sanitize(name: str) -> str:
  """Sanitize instrument keys for safe filename generation."""
  return (
      name.replace("|", "_")
      .replace(" ", "_")
      .replace("/", "_")
      .replace(":", "_")
  )


def fetch_and_save_candles():
  api_instance = upstox_client.HistoryV3Api()

  print("=" * 70)
  print("FETCHING UPSTOX INTRADAY CANDLES")
  print("=" * 70)
  print(f"Output Directory : {OUTPUT_DIR.resolve()}")
  print(f"Instruments      : {INSTRUMENT_LIST}")
  print(f"Intervals        : {INTERVALS}")
  print("=" * 70 + "\n")

  for instrument_key in INSTRUMENT_LIST:
    for interval in INTERVALS:
      print(
          f"Fetching intraday data for {instrument_key} (Interval:"
          f" {interval}m)..."
      )
      try:
        # Call Upstox HistoryV3Api for intraday candle data
        response = api_instance.get_intra_day_candle_data(
            instrument_key=instrument_key,
            interval=interval,
            unit="minutes",
        )

        # Build output filename
        sanitized_name = sanitize(instrument_key)
        output_file = OUTPUT_DIR / f"{sanitized_name}__intraday_{interval}m.json"

        # Convert response object/dict to serializable format
        # If response is an SDK model, you might need to use its to_dict() method or parse its attributes
        data_to_save = (
            response.to_dict() if hasattr(response, "to_dict") else response
        )

        # Save to JSON with indent=4 and UTF-8 encoding
        with open(output_file, "w", encoding="utf-8") as f:
          json.dump(data_to_save, f, indent=4, ensure_ascii=False)

        print(
            f"  [SUCCESS] Saved intraday data to {output_file.name}\n"
        )

      except Exception as e:
        print(
            f"  [ERROR] Failed to fetch {instrument_key} at interval"
            f" {interval}min: {e}\n"
        )

  print("=" * 70)
  print("BATCH FETCH COMPLETED")
  print("=" * 70)


if __name__ == "__main__":
  fetch_and_save_candles()