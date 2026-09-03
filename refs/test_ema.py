from services.opening_range.ema_alerts import (
    build_isolated_ema_telegram_message,
)

payload = {
    "instrument": {
        "instrument_key": "NSE_FO|42645",
        "instrument_type": "CE",
        "strike_price": 24150,
    },
    "opening_range": {
        "selected_level": "R3",
    },
    "market_snapshot": {
        "nifty_ltp": 23913.95,
    },
    "ema": {
        "cross_type": "bullish_cross",
        "calculation_mode": "candle_close",
        "current_signal": None,
        "timestamp": "2026-09-03T12:40:00+05:30",
        "candle": {
            "timestamp": "2026-09-03T12:40:00+05:30",
            "close": 41.15,
            "low": 40.50,
            "close_minus_low_points": 0.65,
        },
    },
    "order_suggestion": {
        "suggested_order_side": "CE",
        "nearest_instruments": [
            {
                "strike_price": 23850,
                "option_type": "CE",
                "ltp": 176.55,
                "volume": 14424930,
            },
            {
                "strike_price": 23900,
                "option_type": "CE",
                "ltp": 145.20,
                "volume": 57781360,
            },
            {
                "strike_price": 23950,
                "option_type": "CE",
                "ltp": 117.10,
                "volume": 66940705,
            },
        ],
        "budget_filter": {
            "instruments": [
                {
                    "strike_price": 24000,
                    "option_type": "CE",
                    "ltp": 93.30,
                },
                {
                    "strike_price": 24050,
                    "option_type": "CE",
                    "ltp": 72.50,
                },
                {
                    "strike_price": 24100,
                    "option_type": "CE",
                    "ltp": 55.50,
                },
            ]
        },
    },
}

message = build_isolated_ema_telegram_message(
    payload
)

print(message)


# myenv\Scripts\python.exe test_telegram.py