import json
import requests


# ============================================================
# 1. Load access token from data.json
# ============================================================

with open("data.json", "r", encoding="utf-8") as file:
    data = json.load(file)

access_token = data.get("access_token")

if not access_token:
    raise ValueError(
        "access_token not found in data.json"
    )


# ============================================================
# 2. Configuration
# ============================================================

instrument_key = "NSE_FO|56957|22-09-2026"
interval = "1minute"

from_date = "2026-09-11"
to_date = "2026-09-21"

output_file = "output1.json"


# ============================================================
# 3. Build Upstox API URL
# ============================================================

url = (
    "https://api.upstox.com/v2/"
    "expired-instruments/historical-candle/"
    f"{instrument_key}/"
    f"{interval}/"
    f"{to_date}/"
    f"{from_date}"
)


# ============================================================
# 4. Headers
# ============================================================

headers = {
    "Authorization": f"Bearer {access_token}",
    "Accept": "application/json"
}


# ============================================================
# 5. Print request information
# ============================================================

print("=" * 60)
print("UPSTOX EXPIRED HISTORICAL CANDLE DATA")
print("=" * 60)

print(f"Instrument : {instrument_key}")
print(f"Interval   : {interval}")
print(f"From Date  : {from_date}")
print(f"To Date    : {to_date}")
print(f"Output     : {output_file}")

print("=" * 60)
print("Fetching data...")


# ============================================================
# 6. Execute GET request
# ============================================================

try:

    response = requests.get(
        url,
        headers=headers,
        timeout=60
    )


    # ========================================================
    # 7. Handle response
    # ========================================================

    if response.status_code == 200:

        response_data = response.json()

        print()
        print("Data retrieved successfully.")


        # ====================================================
        # 8. Save complete API response
        # ====================================================

        with open(
            output_file,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                response_data,
                file,
                indent=2,
                ensure_ascii=False
            )


        # ====================================================
        # 9. Show candle count
        # ====================================================

        candles = (
            response_data
            .get("data", {})
            .get("candles", [])
        )

        print()
        print("=" * 60)
        print("SUCCESS")
        print("=" * 60)
        print(f"HTTP Status : {response.status_code}")
        print(f"Candles     : {len(candles)}")
        print(f"Saved       : {output_file}")
        print("=" * 60)


    else:

        print()
        print("=" * 60)
        print("API ERROR")
        print("=" * 60)

        print(f"HTTP Status : {response.status_code}")
        print(f"Response    : {response.text}")

        print("=" * 60)


except requests.exceptions.RequestException as e:

    print()
    print("=" * 60)
    print("REQUEST ERROR")
    print("=" * 60)

    print(e)

    print("=" * 60)