"""Admin margin-check routes.

    POST /admin/orders/margin        -> check margin for one instrument
    POST /admin/orders/margin/batch  -> check margin for several instruments

Request body (single)::

    {
      "instrument_key": "NSE_FO|47162",   # required
      "lots": 1,                          # default 1
      "transaction_type": "BUY",          # default BUY
      "product": "D",                     # default D (intraday)
      "price": 0.0,
      "verbose": false                    # default false
    }

Request body (batch)::

    {
      "instruments": [
        {"instrument_key": "NSE_FO|47162", "lots": 1},
        {"instrument_key": "BSE_FO|SENSEX_KEY", "lots": 2, "transaction_type": "SELL"}
      ]
    }

Each item may also carry its own ``verbose`` flag; the top-level
``verbose`` (if provided) is used as the default for items that do not
specify one.

Response
--------
Both endpoints return HTTP 200 with the same shape as the
``check_margin()`` function:

    {
      "status": "ok",
      "instrument_key": "...",
      "underlying": "...",
      "transaction_type": "BUY",
      "lots": 1,
      "lot_size": 65,
      "lot_source": "exchange_default",
      "quantity": 65,
      "product": "D",
      "required_margin": 1898.0,
      "final_margin": 1898.0,
      "error": null,
      "called_at": "2026-10-19T…"
    }

When ``verbose: true`` the response additionally carries a **sanitised**
``response`` dict. A missing ``instrument_key`` returns HTTP 400 so
callers can distinguish bad input from a provider failure.

Security
--------
No access token, no account identifier, and no ``token_masked`` field
appears in any response. Provider error bodies are passed through
``upstox_services._sanitise.sanitise_payload`` before they leave the
service (see ``upstox_services/margin_check.py``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from upstox_services.margin_check import check_margin

router = APIRouter(prefix="/admin/orders", tags=["orders"])


def _require_instrument_key(payload: dict[str, Any]) -> str:
    key = str(payload.get("instrument_key") or "").strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "instrument_key_required",
                "message": "instrument_key is mandatory.",
            },
        )
    return key


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


@router.post("/margin")
async def margin_single(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    key = _require_instrument_key(payload)
    return await asyncio.to_thread(
        check_margin,
        key,
        lots=int(payload.get("lots") or 1),
        transaction_type=str(payload.get("transaction_type") or "BUY"),
        product=str(payload.get("product") or "D"),
        price=float(payload.get("price") or 0.0),
        verbose=_coerce_bool(payload.get("verbose"), default=False),
    )


@router.post("/margin/batch")
async def margin_batch(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    items = payload.get("instruments")
    if not isinstance(items, list) or not items:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "instruments_required",
                "message": "Body must contain a non-empty `instruments` list.",
            },
        )

    top_verbose = _coerce_bool(payload.get("verbose"), default=False)

    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            results.append(
                {
                    "status": "invalid_input",
                    "error": "each item must be an object",
                    "instrument_key": None,
                }
            )
            continue
        key = str(item.get("instrument_key") or "").strip()
        if not key:
            results.append(
                {
                    "status": "invalid_input",
                    "error": "instrument_key is required",
                    "instrument_key": None,
                }
            )
            continue

        item_verbose = (
            _coerce_bool(item.get("verbose"), default=False)
            if "verbose" in item
            else top_verbose
        )

        try:
            lots_val = int(item.get("lots") or 1)
        except (TypeError, ValueError):
            lots_val = 1
        try:
            price_val = float(item.get("price") or 0.0)
        except (TypeError, ValueError):
            price_val = 0.0

        results.append(
            await asyncio.to_thread(
                check_margin,
                key,
                lots=lots_val,
                transaction_type=str(item.get("transaction_type") or "BUY"),
                product=str(item.get("product") or "D"),
                price=price_val,
                verbose=item_verbose,
            )
        )

    return {
        "count": len(results),
        "verbose": top_verbose,
        "results": results,
    }