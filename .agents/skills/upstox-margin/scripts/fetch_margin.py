"""Fetch Upstox margin and/or brokerage, optionally pricing from one live LTP snapshot."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "upstox" / "scripts"))
from upstox_helpers import get_client  # noqa: E402


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        return to_plain(converter())
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {key: to_plain(item) for key, item in attrs.items()
                if not key.startswith("_")}
    return str(value)


def response_data(response: Any) -> Any:
    plain = to_plain(response)
    return plain.get("data", plain) if isinstance(plain, dict) else plain


def fetch_market_price(client: Any, instrument_key: str) -> tuple[float, Any]:
    import upstox_client

    response = upstox_client.MarketQuoteV3Api(client).get_ltp(
        instrument_key=instrument_key
    )
    raw = to_plain(response)
    quotes = raw.get("data", {}) if isinstance(raw, dict) else {}
    if not isinstance(quotes, dict) or instrument_key not in quotes:
        available = list(quotes.keys()) if isinstance(quotes, dict) else []
        raise ValueError(
            f"LTP response did not contain the requested instrument {instrument_key!r}. "
            f"Returned keys: {available}"
        )
    quote = quotes[instrument_key]
    if not isinstance(quote, dict) or "last_price" not in quote:
        raise ValueError("LTP response is missing last_price for the requested instrument.")
    price = float(quote["last_price"])
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"Upstox returned an invalid LTP: {quote['last_price']!r}")
    return price, raw


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Upstox margin and brokerage estimates."
    )
    parser.add_argument(
        "--kind",
        choices=["required-margin", "brokerage", "both"],
        required=True,
    )
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--transaction-type", choices=["BUY", "SELL"], required=True)
    parser.add_argument("--product", choices=["I", "D", "CO", "OCO", "MTF"], required=True)
    parser.add_argument(
        "--price",
        required=True,
        help="Numeric price, or 'market' to fetch the current LTP once.",
    )
    args = parser.parse_args()

    requested_price = args.price
    result_request = {
        "instrument": args.instrument,
        "quantity": args.quantity,
        "transaction_type": args.transaction_type,
        "product": args.product,
        "requested_price": requested_price,
        "price": None,
        "price_source": None,
    }

    def emit(payload: dict[str, Any]) -> int:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0

    try:
        if args.quantity <= 0:
            raise ValueError("Quantity must be greater than zero.")

        if requested_price.strip().lower() == "market":
            price_source = "LIVE_LTP"
            client = get_client()
            price, quote_raw = fetch_market_price(client, args.instrument)
        else:
            price = float(requested_price)
            if not math.isfinite(price) or price < 0:
                raise ValueError("Price must be finite and non-negative, or 'market'.")
            price_source = "PROVIDED"
            quote_raw = None
            client = get_client()

        result_request["price"] = price
        result_request["price_source"] = price_source

        import upstox_client
        charge_api = upstox_client.ChargeApi(client)

        data: dict[str, Any] = {}
        raw_response: dict[str, Any] = {}
        component_errors: list[dict[str, str]] = []

        if args.kind in ("required-margin", "both"):
            try:
                instrument = upstox_client.Instrument(
                    instrument_key=args.instrument,
                    quantity=args.quantity,
                    transaction_type=args.transaction_type,
                    product=args.product,
                    price=price,
                )
                response = charge_api.post_margin(
                    upstox_client.MarginRequest(instruments=[instrument])
                )
                raw_response["margin"] = to_plain(response)
                data["margin"] = response_data(response)
            except Exception as exc:
                component_errors.append({
                    "component": "margin",
                    "type": type(exc).__name__,
                    "message": str(exc),
                })
                data["margin"] = None

        if args.kind in ("brokerage", "both"):
            try:
                response = charge_api.get_brokerage(
                    instrument_token=args.instrument,
                    quantity=args.quantity,
                    product=args.product,
                    transaction_type=args.transaction_type,
                    price=price,
                    api_version="2.0",
                )
                raw_response["brokerage"] = to_plain(response)
                data["brokerage"] = response_data(response)
            except Exception as exc:
                component_errors.append({
                    "component": "brokerage",
                    "type": type(exc).__name__,
                    "message": str(exc),
                })
                data["brokerage"] = None

        if quote_raw is not None:
            raw_response["market_quote"] = quote_raw

        successful_components = sum(
            1 for key in ("margin", "brokerage")
            if key in data and data[key] is not None
        )
        if component_errors and successful_components:
            status = "partial"
        elif component_errors:
            status = "error"
        else:
            status = "success"

        return emit({
            "status": status,
            "kind": args.kind,
            "request": result_request,
            "data": data,
            "raw_response": raw_response,
            "errors": component_errors,
            "error": None if not component_errors else {
                "type": "CalculationError",
                "message": "One or more calculations failed; see errors.",
            },
        })

    except Exception as exc:
        return emit({
            "status": "error",
            "kind": args.kind,
            "request": result_request,
            "data": None,
            "raw_response": None,
            "errors": [],
            "error": {"type": type(exc).__name__, "message": str(exc)},
        })


if __name__ == "__main__":
    raise SystemExit(main())
