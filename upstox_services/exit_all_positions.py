"""Exit every open position for the Upstox account.

Gated on ``order_runtime_service.is_enabled()`` — returns
``{"status": "disabled"}`` when orders are toggled off.

Standalone CLI:
    python -m upstox_services.exit_all_positions --dry-run
    python -m upstox_services.exit_all_positions
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from services.order_runtime_service import order_runtime_service
from services.token_service import token_service

logger = get_logger(__file__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_token(token: str) -> str:
    token = str(token or "").strip()
    if len(token) <= 20:
        return "***masked***"
    return f"{token[:6]}…{token[-4:]}"


def exit_all_positions(dry_run: bool = False) -> dict[str, Any]:
    called_at = _now_iso()

    if not order_runtime_service.is_enabled():
        logger.warning("[exit-all] disabled by runtime toggle")
        return {
            "status": "disabled",
            "error": "orders_disabled",
            "dry_run": bool(dry_run),
            "called_at": called_at,
        }

    try:
        access_token = token_service.get_access_token()
    except Exception as exc:
        logger.exception("[exit-all] token lookup raised")
        return {
            "status": "error",
            "error": f"token_lookup_failed: {type(exc).__name__}: {exc}",
            "dry_run": bool(dry_run),
            "called_at": called_at,
        }

    if not access_token:
        return {
            "status": "no_token",
            "error": "access_token_missing",
            "dry_run": bool(dry_run),
            "called_at": called_at,
        }

    token_masked = _mask_token(access_token)

    if dry_run:
        logger.info("[exit-all] DRY RUN token=%s", token_masked)
        return {
            "status": "dry_run",
            "dry_run": True,
            "token_masked": token_masked,
            "called_at": called_at,
        }

    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = access_token
        api_instance = upstox_client.OrderApi(
            upstox_client.ApiClient(configuration)
        )
        raw_response = api_instance.exit_positions()
    except ApiException as exc:
        http_status = getattr(exc, "status", None)
        body_preview = str(getattr(exc, "body", None) or exc)[:300]
        logger.warning(
            "[exit-all] ApiException (HTTP %s): %s",
            http_status, body_preview,
        )
        return {
            "status": "error",
            "error": f"Upstox ApiException (HTTP {http_status}): {body_preview}",
            "http_status": http_status,
            "token_masked": token_masked,
            "called_at": called_at,
        }
    except Exception as exc:
        logger.exception("[exit-all] unexpected error")
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "token_masked": token_masked,
            "called_at": called_at,
        }

    logger.info("[exit-all] OK")
    return {
        "status": "ok",
        "dry_run": False,
        "token_masked": token_masked,
        "response": (
            raw_response if isinstance(raw_response, dict)
            else (raw_response.to_dict() if hasattr(raw_response, "to_dict") else {"raw": str(raw_response)})
        ),
        "called_at": called_at,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Exit all open Upstox positions.")
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    result = exit_all_positions(dry_run=args.dry_run)
    status = result.get("status")
    if status in {"ok", "dry_run"}:
        print(f"{status.upper()}: {result}")
        return 0
    print(f"ERROR ({status}): {result.get('error')}")
    return 2 if status == "no_token" else 1


# if __name__ == "__main__":
#     sys.exit(main())