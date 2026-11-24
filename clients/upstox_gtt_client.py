"""Dedicated Upstox v3 GTT adapter, based on algocli_v1 SDK patterns."""
from __future__ import annotations
from typing import Any
import upstox_client
from core.config import LIVE_GTT_SANDBOX, UPSTOX_API_VERSION
from core.token_service import fetch_access_token


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        return to_plain(converter())
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {k: to_plain(v) for k, v in attrs.items() if not k.startswith("_")}
    return str(value)


class UpstoxGTTClient:
    """Create a private SDK client per worker; never share it with feed loops."""
    def __init__(self):
        import os
        config = upstox_client.Configuration(sandbox=LIVE_GTT_SANDBOX)
        token = (
            os.getenv("UPSTOX_SANDBOX_ACCESS_TOKEN", "").strip()
            if LIVE_GTT_SANDBOX else fetch_access_token()
        )
        if not token:
            raise RuntimeError("No Upstox access token is available for the selected environment")
        config.access_token = token
        self.client = upstox_client.ApiClient(config)
        self.gtt_api = upstox_client.OrderApiV3(self.client)
        self.order_api = upstox_client.OrderApi(self.client)
        self.quote_api = upstox_client.MarketQuoteV3Api(self.client)
        self.portfolio_api = upstox_client.PortfolioApi(self.client)

    @staticmethod
    def data(response):
        return to_plain(getattr(response, "data", response))

    def get_ltp(self, instrument_key: str) -> dict:
        data = self.data(self.quote_api.get_ltp(instrument_key=instrument_key))
        candidate = data.get(instrument_key) if isinstance(data, dict) else None
        if candidate is None and isinstance(data, dict) and isinstance(data.get("data"), dict):
            candidate = data["data"].get(instrument_key)
        candidate = candidate or data
        if not isinstance(candidate, dict):
            raise ValueError("Unexpected Upstox LTP response")
        price = candidate.get("last_price", candidate.get("ltp"))
        if price is None:
            raise ValueError(f"No last_price returned for {instrument_key}")
        return {"last_price": float(price), "provider_timestamp": candidate.get("timestamp"), "raw": data}

    def place_gtt_bracket(self, instrument_key, quantity, product, entry, stop, target):
        rules = [
            upstox_client.GttRule(strategy="ENTRY", trigger_type="IMMEDIATE", trigger_price=float(entry)),
            upstox_client.GttRule(strategy="STOPLOSS", trigger_type="IMMEDIATE", trigger_price=float(stop)),
            upstox_client.GttRule(strategy="TARGET", trigger_type="IMMEDIATE", trigger_price=float(target)),
        ]
        body = upstox_client.GttPlaceOrderRequest(
            type="MULTIPLE", instrument_token=instrument_key, product=product,
            quantity=int(quantity), rules=rules, transaction_type="BUY",
        )
        return to_plain(self.gtt_api.place_gtt_order(body=body))

    def get_gtt(self, gtt_id: str) -> dict:
        data = self.data(self.gtt_api.get_gtt_order_details(gtt_order_id=gtt_id))
        if isinstance(data, list):
            rows = [x for x in data if isinstance(x, dict) and x.get("gtt_order_id") == gtt_id]
            if len(rows) != 1:
                raise ValueError(f"Expected one GTT record for {gtt_id}; got {len(rows)}")
            return rows[0]
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            rows = [x for x in data["data"] if isinstance(x, dict) and x.get("gtt_order_id") == gtt_id]
            if len(rows) != 1:
                raise ValueError(f"Expected one GTT record for {gtt_id}; got {len(rows)}")
            return rows[0]
        if not isinstance(data, dict):
            raise ValueError("Unexpected GTT detail response")
        return data

    def cancel_gtt(self, gtt_id: str):
        body = upstox_client.GttCancelOrderRequest(gtt_order_id=gtt_id)
        return to_plain(self.gtt_api.cancel_gtt_order(body=body))

    def modify_gtt_stop(self, gtt_id: str, stop: float):
        current = self.get_gtt(gtt_id)
        rules = current.get("rules")
        if not isinstance(rules, list):
            raise ValueError("GTT rules are unavailable; refusing to modify")
        indices = [i for i, r in enumerate(rules) if str(r.get("strategy", "")).upper() == "STOPLOSS"]
        if len(indices) != 1:
            raise ValueError(f"Expected exactly one STOPLOSS rule; found {len(indices)}")
        updated = [dict(r) for r in rules]
        updated[indices[0]]["trigger_price"] = float(stop)
        sdk_rules = []
        for rule in updated:
            fields = {k: rule[k] for k in ("strategy", "trigger_type", "trigger_price", "trailing_gap", "market_protection") if k in rule}
            sdk_rules.append(upstox_client.GttRule(**fields))
        body = upstox_client.GttModifyOrderRequest(
            type=current["type"], gtt_order_id=gtt_id,
            quantity=int(current["quantity"]), rules=sdk_rules,
        )
        response = to_plain(self.gtt_api.modify_gtt_order(body=body))
        return {"modify_response": response, "verified_gtt": self.get_gtt(gtt_id)}

    def get_positions(self) -> list[dict]:
        data = self.data(self.portfolio_api.get_positions(api_version=UPSTOX_API_VERSION))
        if isinstance(data, dict):
            data = data.get("data", data.get("positions", []))
        if not isinstance(data, list):
            raise ValueError("Unexpected positions response")
        return data

    def place_market_exit(self, instrument_key, quantity, product, tag):
        body = upstox_client.PlaceOrderV3Request(
            quantity=int(quantity), product=product, validity="DAY", price=0.0,
            tag=tag[:40], instrument_token=instrument_key, order_type="MARKET",
            transaction_type="SELL", disclosed_quantity=0, trigger_price=0.0, is_amo=False,
        )
        return to_plain(self.gtt_api.place_order(body))

    def get_order_details(self, order_id):
        data = self.data(self.order_api.get_order_details(
            api_version=UPSTOX_API_VERSION, order_id=order_id
        ))
        if isinstance(data, list):
            if not data:
                raise ValueError(f"No order details for {order_id}")
            return data[-1]
        if not isinstance(data, dict):
            raise ValueError("Unexpected order details response")
        return data
