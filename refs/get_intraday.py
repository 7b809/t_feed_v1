import os
import json
import upstox_client

# Instrument
instrument_key = "NSE_FO|45116"

# Output folder
output_dir = "output"
os.makedirs(output_dir, exist_ok=True)

# Safe filename for Windows
safe_filename = instrument_key.replace("|", "_")

output_file = os.path.join(
    output_dir,
    f"{safe_filename}.json"
)

# Upstox API
api_instance = upstox_client.HistoryV3Api()

try:
    response = api_instance.get_intra_day_candle_data(
        instrument_key,
        "minutes",
        "1"
    )

    # Convert Upstox response object to dictionary
    if hasattr(response, "to_dict"):
        data = response.to_dict()
    else:
        data = response

    # Save response
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
            default=str
        )

    print(f"Saved successfully: {output_file}")

except Exception as e:
    print(f"Exception when calling HistoryV3Api: {e}")