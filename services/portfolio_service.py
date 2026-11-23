"""Live Upstox positions and holdings for the broker-sourced dashboard."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from core.config import TIMEZONE, UPSTOX_HTTP_TIMEOUT_SECONDS, UPSTOX_SANDBOX
from core.logger import get_logger
from core.token_service import fetch_access_token

logger = get_logger(__file__)
POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"
HOLDINGS_URL = "https://api.upstox.com/v2/portfolio/long-term-holdings"


class PortfolioServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


def _fetch_rows(url: str, resource: str) -> list[dict[str, Any]]:
    if UPSTOX_SANDBOX:
        raise PortfolioServiceError(
            "Live Upstox portfolio endpoints require UPSTOX_SANDBOX=false.",
            status_code=409,
        )
    try:
        token = fetch_access_token()
        response = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=UPSTOX_HTTP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("Upstox %s request failed: %s", resource, type(exc).__name__)
        raise PortfolioServiceError(f"Could not retrieve Upstox {resource}.", status_code=503) from None

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok:
        logger.warning("Upstox %s request returned HTTP %s", resource, response.status_code)
        raise PortfolioServiceError(
            f"Upstox rejected the {resource} request.",
            status_code=502,
            details={"upstream_http_status": response.status_code},
        )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or str(payload.get("status", "")).lower() != "success"
        or not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise PortfolioServiceError(f"Upstox returned an invalid {resource} response.", status_code=502)
    return rows


def _number(row: dict[str, Any], field: str, resource: str, index: int, *, default: float | None = None) -> float:
    value = row.get(field)
    if value is None:
        if default is not None:
            return default
        raise PortfolioServiceError(
            f"Upstox omitted {field} in {resource} row {index}.",
            status_code=502,
            details={"resource": resource, "row_index": index, "field": field},
        )
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PortfolioServiceError(
            f"Upstox returned an invalid {field} in {resource} row {index}.",
            status_code=502,
            details={"resource": resource, "row_index": index, "field": field},
        ) from None
    if not math.isfinite(number):
        raise PortfolioServiceError(
            f"Upstox returned a non-finite {field} in {resource} row {index}.",
            status_code=502,
            details={"resource": resource, "row_index": index, "field": field},
        )
    return number


def get_positions() -> dict[str, Any]:
    rows = _fetch_rows(POSITIONS_URL, "positions")
    return {"status": "success", "source": "upstox", "count": len(rows), "data": rows}


def get_holdings() -> dict[str, Any]:
    rows = _fetch_rows(HOLDINGS_URL, "holdings")
    return {"status": "success", "source": "upstox", "count": len(rows), "data": rows}


def get_portfolio_overview() -> dict[str, Any]:
    """Return both broker lists and separately defined daily/combined P&L totals."""
    errors: dict[str, str] = {}
    positions: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    try:
        positions = _fetch_rows(POSITIONS_URL, "positions")
    except PortfolioServiceError as exc:
        errors["positions"] = str(exc)
    except Exception:
        logger.exception("Unexpected failure fetching Upstox positions")
        errors["positions"] = "Unable to load Upstox positions."

    try:
        holdings = _fetch_rows(HOLDINGS_URL, "holdings")
    except PortfolioServiceError as exc:
        errors["holdings"] = str(exc)
    except Exception:
        logger.exception("Unexpected failure fetching Upstox holdings")
        errors["holdings"] = "Unable to load Upstox holdings."

    realised_pnl = unrealised_pnl = today_pnl = None
    holdings_pnl = holdings_day_change = None
    if "positions" not in errors:
        realised_pnl = round(sum(_number(row, "realised", "positions", i) for i, row in enumerate(positions)), 2)
        unrealised_pnl = round(sum(_number(row, "unrealised", "positions", i) for i, row in enumerate(positions)), 2)
        today_pnl = round(realised_pnl + unrealised_pnl, 2)
    if "holdings" not in errors:
        holdings_pnl = round(sum(_number(row, "pnl", "holdings", i) for i, row in enumerate(holdings)), 2)
        holdings_day_change = round(sum(_number(row, "day_change", "holdings", i, default=0.0) for i, row in enumerate(holdings)), 2)

    # Combined portfolio snapshot is populated only when both broker sources are available.
    total_pnl = round(today_pnl + holdings_pnl, 2) if today_pnl is not None and holdings_pnl is not None else None
    return {
        "status": "success" if not errors else "partial",
        "source": "upstox",
        "updated_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
        "positions_count": len(positions) if "positions" not in errors else None,
        "holdings_count": len(holdings) if "holdings" not in errors else None,
        "realised_pnl": realised_pnl,
        "unrealised_pnl": unrealised_pnl,
        "today_pnl": today_pnl,
        "holdings_pnl": holdings_pnl,
        "holdings_day_change": holdings_day_change,
        "total_pnl": total_pnl,
        "total_pnl_definition": "Today's short-term position P&L (realised + unrealised) plus Upstox-reported long-term holdings P&L.",
        "positions": positions,
        "holdings": holdings,
        "errors": errors,
    }
