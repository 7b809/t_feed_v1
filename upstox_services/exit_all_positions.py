# File: upstox_services/exit_all_positions.py
"""Exit every open position for the Upstox account.

Gated on ``order_runtime_service.is_enabled()`` — returns
``{"status": "disabled"}`` when orders are toggled off.

Standalone CLI:
    python -m upstox_services.exit_all_positions --dry-run
    python -m upstox_services.exit_all_positions

Security guarantees (2026-10-22 token-leak fix)
-----------------------------------------------
The project's security policy is absolute: **no access token — masked
or otherwise — appears in any log line or returned dict.**

The previous revision violated this in four places:

  * ``logger.info("[exit-all] DRY RUN token=%s", token_masked)`` — a
    masked token was written to the log on every dry run;
  * the ``dry_run`` return dict carried ``"token_masked": token_masked``;
  * the ``ApiException`` error return dict carried ``"token_masked"``;
  * the ``ok`` return dict carried ``"token_masked"``.

This revision removes all four:

  * the token is read once from ``token_service`` and held only in a
    local variable, for the duration of the call;
  * ``logger.info`` / ``logger.warning`` calls never include the token
    or any masked form of it;
  * no returned dict carries a ``token_masked`` key;
  * provider error bodies and verbose SDK responses are passed through
    ``upstox_services._sanitise.sanitise_payload`` before being placed
    in a log line or return dict, so a provider-side error body cannot
    leak an ``Authorization`` header or a bearer token.

The ``_mask_token`` helper is kept as a module-level function for
backward compatibility with any external import, but it is no longer
called from ``exit_all_positions`` — the function no longer has a
token to mask.
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
from upstox_services._sanitise import sanitise_payload

logger = get_logger(__file__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_token(token: str) -> str:
    """Return a masked token for callers that explicitly want one.

    NOTE: ``exit_all_positions`` does **not** use this helper and does
    not emit a masked token anywhere. It is kept here only for
    backward compatibility with external imports that may still call
    it.
    """
    token = str(token or "").strip()
    if len(token) <= 20:
        return "***masked***"
    return f"{token[:6]}…{token[-4:]}"


def _summarise_api_response(raw: Any) -> dict[str, Any]:
    """Return a sanitised, JSON-serialisable summary of the Upstox response.

    Every branch is passed through ``sanitise_payload`` so a provider
    body that happens to embed an ``Authorization`` header or a
    bearer token cannot leak through the return dict.
    """
    if raw is None:
        return {"raw": None}
    if isinstance(raw, dict):
        return sanitise_payload(raw)
    if hasattr(raw, "to_dict"):
        try:
            return sanitise_payload(raw.to_dict())
        except Exception:
            pass
    try:
        return sanitise_payload(dict(raw))
    except Exception:
        return {"raw": sanitise_payload(str(raw))}


def exit_all_positions(dry_run: bool = False) -> dict[str, Any]:
    """Exit every open position for the Upstox account.

    Returns a dict with ``status`` in
    ``{"ok", "disabled", "no_token", "dry_run", "error"}``.

    Security: the access token never appears in any log line or in
    the returned dict. Provider error bodies are redacted by
    ``sanitise_payload`` before they leave this function.
    """
    called_at = _now_iso()

    # ---- 0. Runtime toggle ----------------------------------------- #
    if not order_runtime_service.is_enabled():
        logger.warning("[exit-all] disabled by runtime toggle")
        return {
            "status": "disabled",
            "error": "orders_disabled",
            "dry_run": bool(dry_run),
            "called_at": called_at,
        }

    # ---- 1. Token -------------------------------------------------- #
    # Read once. Held only in this local variable for the duration of
    # the call. Never logged, never returned, never interpolated into
    # an error string.
    try:
        access_token = token_service.get_access_token()
    except Exception as exc:
        logger.exception("[exit-all] token lookup raised")
        return {
            "status": "error",
            "error": f"token_lookup_failed: {type(exc).__name__}",
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

    # ---- 2. Dry run: return without calling the API ---------------- #
    # SECURITY: the previous revision logged ``token=<masked>`` here.
    # A masked token is still sensitive because it reveals the token
    # prefix and suffix. The new log line contains no token at all.
    if dry_run:
        logger.info(
            "[exit-all] DRY RUN — order runtime enabled, token present, "
            "no API call made"
        )
        return {
            "status": "dry_run",
            "dry_run": True,
            "runtime_enabled": True,
            "token_present": True,
            "called_at": called_at,
        }

    # ---- 3. Call -------------------------------------------------- #
    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = access_token
        api_instance = upstox_client.OrderApi(
            upstox_client.ApiClient(configuration)
        )
        raw_response = api_instance.exit_positions()
    except ApiException as exc:
        http_status = getattr(exc, "status", None)
        # Redact any credential-like run in the provider body before
        # it reaches a log line or the return dict.
        body_preview = sanitise_payload(
            str(getattr(exc, "body", None) or exc)
        )[:300]
        logger.warning(
            "[exit-all] ApiException (HTTP %s): %s",
            http_status, body_preview,
        )
        return {
            "status": "error",
            "error": (
                f"Upstox ApiException (HTTP {http_status}): "
                f"{body_preview}"
            ),
            "http_status": http_status,
            # SECURITY: no ``token_masked`` field.
            "called_at": called_at,
        }
    except Exception as exc:
        logger.exception("[exit-all] unexpected error")
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            # SECURITY: no ``token_masked`` field.
            "called_at": called_at,
        }

    logger.info("[exit-all] OK")
    # SECURITY: no ``token_masked`` field. The ``response`` payload is
    # the sanitised summary produced by ``_summarise_api_response``.
    return {
        "status": "ok",
        "dry_run": False,
        "response": _summarise_api_response(raw_response),
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


if __name__ == "__main__":
    sys.exit(main())