import logging
import random
from datetime import datetime

import upstox_client

from core import config
from utils.common import object_to_dict, safe_float, safe_int
from utils.json_store import write_json_atomic

logger = logging.getLogger(__name__)


def normalize_type(value):
    value = str(value or "").upper().strip()
    return "CE" if value in {"CE", "CALL"} else "PE" if value in {"PE", "PUT"} else None


def fetch_and_select(access_token: str) -> tuple[dict, list[dict]]:
    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    client = upstox_client.ApiClient(configuration)
    try:
        response = upstox_client.OptionsApi(client).get_option_contracts(config.MAIN_NIFTY_SECURITY)
        payload = object_to_dict(response)
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        today = datetime.now(config.MARKET_TIMEZONE).date()
        prepared = []
        expiries = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                expiry = datetime.fromisoformat(str(row.get("expiry"))[:10]).date()
            except ValueError:
                continue
            strike = safe_float(row.get("strike_price"))
            option_type = normalize_type(row.get("option_type") or row.get("instrument_type"))
            key = str(row.get("instrument_key") or "").strip()
            if expiry >= today and strike is not None and config.STRIKE_FROM <= strike <= config.STRIKE_TO and option_type and key:
                expiries.append(expiry)
                prepared.append({
                    "instrument_key": key, "instrument_type": option_type, "option_type": option_type,
                    "strike_price": strike, "expiry": expiry.isoformat(), "trading_symbol": row.get("trading_symbol"),
                    "underlying_type": row.get("underlying_type"), "underlying_symbol": row.get("underlying_symbol"),
                    "lot_size": safe_int(row.get("lot_size")),
                })
        if not expiries:
            raise RuntimeError("No valid nearest-expiry NIFTY contracts found")
        nearest = min(expiries).isoformat()
        unique = {row["instrument_key"]: row for row in prepared if row["expiry"] == nearest}
        valid = list(unique.values())
        selected = valid
        if config.TEST_FLAG:
            selected = random.Random(config.TEST_RANDOM_SEED).sample(valid, min(config.TEST_ITEM_COUNT, len(valid)))
        output = {
            "status": "success", "generated_at": datetime.now(config.MARKET_TIMEZONE).isoformat(),
            "nearest_expiry": nearest, "strike_from": config.STRIKE_FROM, "strike_to": config.STRIKE_TO,
            "test_flag": config.TEST_FLAG, "test_item_count": config.TEST_ITEM_COUNT,
            "test_random_seed": config.TEST_RANDOM_SEED, "total_contracts": len(valid), "data": valid,
        }
        selected_output = {**output, "total_contracts": len(selected), "data": selected}
        write_json_atomic(config.CONTRACTS_FILE, output)
        write_json_atomic(config.RUNTIME_ROOT / "selected_contracts.json", selected_output)
        for item in selected:
            logger.info("Selected instrument=%s strike=%s type=%s", item["instrument_key"], item["strike_price"], item["option_type"])
        return selected_output, selected

    finally:
        close_method = getattr(client, "close", None)

        if callable(close_method):
            close_method()
