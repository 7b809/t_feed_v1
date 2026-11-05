# upstox_app/services/exit_all_positions.py
"""Exit open Upstox positions — selective by default, nuclear on demand.

Behaviour
---------
Default path (``include_locked=False``)
    Fetches every open position, splits them against the persistent
    lock registry, and places an **individual opposite MARKET order**
    for each non‑locked position using its exact portfolio quantity.
    Locked positions are reported in ``skipped_locked`` and are *not*
    touched.

Nuclear path (``include_locked=True`` + ``confirm="CONFIRM_EXIT_LOCKED"``)
    Requires the exact confirmation token. When supplied, the bulk
    ``OrderApi.exit_positions()`` endpoint is called, which closes
    every open position — including locked ones.

Anything else with ``include_locked=True`` returns
``{"status": "error", "error": "confirmation_required"}``.

Lock-registry failure mode (2026-11-05)
---------------------------------------
The lock registry is now MongoDB-backed. If MongoDB is unreachable
when the exit runs, ``filter_positions`` returns
``{"status": "error", "error": "mongodb_unavailable"}`` and this
service **refuses to exit** — returning
``{"status": "error", "error": "lock_registry_unavailable: ..."}``.
That's the fail-closed behaviour: a registry outage must not become
a license to liquidate positions the operator marked as protected.

The bulk ("nuclear") path is unaffected — ``include_locked=True``
explicitly overrides the registry, so a Mongo outage does not block
an operator who typed the confirmation token.

Return shape
------------
::

    {
      "status": "ok" | "partial" | "disabled" | "no_token"
              | "dry_run" | "error",
      "dry_run": bool,
      "include_locked": bool,
      "positions_total": int,
      "to_exit_count": int,
      "skipped_locked_count": int,
      "success_count": int,
      "fail_count": int,
      "results": [...],
      "skipped_locked": [...],
      "called_at": "..."
    }

Security
--------
The access token never appears in any log line or returned dict.
Provider error bodies are redacted by ``sanitise_payload``.
"""

from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime, timezone
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from services.order_runtime_service import order_runtime_service
from services.telegram_service import telegram_service
from services.token_service import token_service
from upstox_app.services._sanitise import sanitise_payload
from upstox_app.services.get_positions import get_positions
from upstox_app.services.place_order import place_order
from upstox_app.services.position_lock_service import filter_positions

logger = get_logger(__file__)

CONFIRM_EXIT_LOCKED = "CONFIRM_EXIT_LOCKED"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_token(token: str) -> str:
    """Backward-compat only; this module never calls it."""
    token = str(token or "").strip()
    if len(token) <= 20:
        return "***masked***"
    return f"{token[:6]}…{token[-4:]}"


def _summarise_api_response(raw: Any) -> dict[str, Any]:
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


# --------------------------------------------------------------------------- #
# Telegram helpers — best-effort, never raise                                 #
# --------------------------------------------------------------------------- #
def _tg_escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _send_exit_started(
    *, to_exit: int, skipped_locked: int, include_locked: bool
) -> None:
    try:
        title = "⏳ Exit positions started"
        mode = (
            "ALL positions (including locked)"
            if include_locked else "non-locked positions only"
        )
        body = (
            f"Exiting <b>{to_exit}</b> position(s) — {_tg_escape(mode)}.\n"
            f"Skipped locked: <b>{skipped_locked}</b>"
        )
        telegram_service.send_message(title, body, level="WARNING")
    except Exception:
        logger.exception("[exit-all] telegram 'started' notify failed")


def _send_exit_completed(
    *, success_count: int, fail_count: int,
    skipped_locked: int, failures: list[dict[str, Any]],
) -> None:
    try:
        title = "✅ Exit positions completed"
        lines = [
            f"<b>Succeeded</b>: {success_count}",
            f"<b>Failed</b>: {fail_count}",
            f"<b>Skipped (locked)</b>: {skipped_locked}",
        ]
        if failures:
            lines.append("\n<b>Failures</b>:")
            for f in failures[:10]:
                token = _tg_escape(f.get("instrument_token") or "-")
                err = _tg_escape(
                    str(f.get("error") or f.get("result", {}).get("error") or "?")
                )
                lines.append(f"• <code>{token}</code> — {err}")
            if len(failures) > 10:
                lines.append(f"• …and {len(failures) - 10} more")
        telegram_service.send_message(title, "\n".join(lines), level="SUCCESS")
    except Exception:
        logger.exception("[exit-all] telegram 'completed' notify failed")


def _send_exit_failed(error_text: str, http_status: Any = None) -> None:
    try:
        title = "❌ Exit positions failed"
        header = ""
        if http_status is not None:
            header = f"<b>HTTP</b>: {_tg_escape(http_status)}\n"
        body = f"{header}<b>Error</b>:\n<pre>{_tg_escape(error_text)}</pre>"
        telegram_service.send_message(title, body, level="ERROR")
    except Exception:
        logger.exception("[exit-all] telegram 'failed' notify failed")


# --------------------------------------------------------------------------- #
# Individual exit path — one opposite MARKET order per position               #
# --------------------------------------------------------------------------- #
def _exit_single_position(position: dict[str, Any]) -> dict[str, Any]:
    token = str(position.get("instrument_token") or "").strip()
    try:
        qty = int(position.get("quantity") or 0)
    except (TypeError, ValueError):
        qty = 0

    if not token:
        return {
            "status": "error",
            "error": "missing_instrument_token",
            "instrument_token": "",
        }
    if qty == 0:
        return {
            "status": "skipped",
            "reason": "zero_quantity",
            "instrument_token": token,
        }

    txn = "SELL" if qty > 0 else "BUY"
    abs_qty = abs(qty)

    logger.info(
        "[exit-all] exiting %s qty=%s txn=%s",
        token, abs_qty, txn,
    )
    result = place_order(
        token,
        quantity=abs_qty,
        transaction_type=txn,
        product=str(position.get("product") or "D"),
        order_type="MARKET",
        tag="EXITPOS",
        notify=False,
    )
    return {
        "status": result.get("status", "error"),
        "instrument_token": token,
        "transaction_type": txn,
        "quantity": abs_qty,
        "result": result,
        "error": result.get("error"),
    }


# --------------------------------------------------------------------------- #
# Nuclear path — bulk exit_positions() endpoint                              #
# --------------------------------------------------------------------------- #
def _exit_all_via_bulk(access_token: str) -> dict[str, Any]:
    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = access_token
        api_instance = upstox_client.OrderApi(
            upstox_client.ApiClient(configuration)
        )
        raw_response = api_instance.exit_positions()
    except ApiException as exc:
        http_status = getattr(exc, "status", None)
        body_preview = sanitise_payload(
            str(getattr(exc, "body", None) or exc)
        )[:300]
        logger.warning(
            "[exit-all] bulk ApiException (HTTP %s): %s",
            http_status, body_preview,
        )
        _send_exit_failed(
            error_text=str(body_preview),
            http_status=http_status,
        )
        return {
            "status": "error",
            "error": f"Upstox ApiException (HTTP {http_status}): {body_preview}",
            "http_status": http_status,
        }
    except Exception as exc:
        logger.exception("[exit-all] bulk unexpected error")
        _send_exit_failed(error_text=f"{type(exc).__name__}: {exc}")
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    logger.info("[exit-all] bulk OK")
    return {
        "status": "ok",
        "response": _summarise_api_response(raw_response),
    }


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #
def exit_all_positions(
    dry_run: bool = False,
    *,
    include_locked: bool = False,
    confirm: str | None = None,
) -> dict[str, Any]:
    """Exit open Upstox positions.

    Default (``include_locked=False``) exits only non-locked positions
    via individual opposite MARKET orders. With ``include_locked=True``
    the exact ``CONFIRM_EXIT_LOCKED`` token is required; when confirmed
    the bulk ``exit_positions()`` endpoint is used.

    Fails closed when the lock registry is unreachable — see the module
    docstring.
    """
    called_at = _now_iso()

    # ---- 0. Runtime toggle ---------------------------------------- #
    if not order_runtime_service.is_enabled():
        logger.warning("[exit-all] disabled by runtime toggle")
        return {
            "status": "disabled",
            "error": "orders_disabled",
            "dry_run": bool(dry_run),
            "called_at": called_at,
        }

    # ---- 1. Confirmation for locked positions ---------------------- #
    if include_locked and str(confirm or "") != CONFIRM_EXIT_LOCKED:
        logger.warning(
            "[exit-all] include_locked requested without confirmation"
        )
        return {
            "status": "error",
            "error": "confirmation_required",
            "hint": (
                f"Pass confirm='{CONFIRM_EXIT_LOCKED}' to exit locked "
                "positions (this is the double-confirmation step)."
            ),
            "dry_run": bool(dry_run),
            "include_locked": True,
            "called_at": called_at,
        }

    # ---- 2. Token ------------------------------------------------- #
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

    # ---- 3. Fetch positions --------------------------------------- #
    pos_result = get_positions()
    if pos_result.get("status") != "ok":
        logger.warning(
            "[exit-all] get_positions failed: %s",
            pos_result.get("error"),
        )
        return {
            "status": "error",
            "error": f"get_positions_failed: {pos_result.get('error')}",
            "dry_run": bool(dry_run),
            "called_at": called_at,
        }

    all_positions = pos_result.get("positions") or []

    # ---- 4. Split against lock registry --------------------------- #
    # Fail closed on lock-registry errors: the registry is the only
    # authority on what must be preserved, and a Mongo outage must
    # never let the exit wipe a protected position.
    if include_locked:
        to_exit = list(all_positions)
        skipped: list[dict[str, Any]] = []
    else:
        filtered = filter_positions(all_positions, include_locked=False)
        if filtered.get("status") == "error":
            err = str(filtered.get("error") or "unknown")
            logger.error(
                "[exit-all] lock registry unavailable — refusing to "
                "exit any position (error=%s)",
                err,
            )
            return {
                "status": "error",
                "error": f"lock_registry_unavailable: {err}",
                "dry_run": bool(dry_run),
                "include_locked": False,
                "positions_total": len(all_positions),
                "called_at": called_at,
            }
        to_exit = filtered["to_exit"]
        skipped = filtered["skipped_locked"]

    # ---- 5. Dry run ----------------------------------------------- #
    if dry_run:
        logger.info(
            "[exit-all] DRY RUN — total=%d to_exit=%d skipped_locked=%d "
            "include_locked=%s",
            len(all_positions), len(to_exit), len(skipped), include_locked,
        )
        return {
            "status": "dry_run",
            "dry_run": True,
            "include_locked": include_locked,
            "positions_total": len(all_positions),
            "to_exit_count": len(to_exit),
            "skipped_locked_count": len(skipped),
            "to_exit": to_exit,
            "skipped_locked": skipped,
            "called_at": called_at,
        }

    # ---- 6. Nothing to do ----------------------------------------- #
    if not to_exit:
        logger.info(
            "[exit-all] nothing to exit (total=%d skipped_locked=%d)",
            len(all_positions), len(skipped),
        )
        return {
            "status": "ok",
            "dry_run": False,
            "include_locked": include_locked,
            "positions_total": len(all_positions),
            "to_exit_count": 0,
            "skipped_locked_count": len(skipped),
            "success_count": 0,
            "fail_count": 0,
            "results": [],
            "skipped_locked": skipped,
            "called_at": called_at,
        }

    # ---- 7. Telegram: "started" ----------------------------------- #
    _send_exit_started(
        to_exit=len(to_exit),
        skipped_locked=len(skipped),
        include_locked=include_locked,
    )

    # ---- 8. Execute ----------------------------------------------- #
    if include_locked:
        bulk = _exit_all_via_bulk(access_token)
        success = len(all_positions) if bulk.get("status") == "ok" else 0
        fail = len(all_positions) - success
        _send_exit_completed(
            success_count=success, fail_count=fail,
            skipped_locked=0, failures=[],
        )
        return {
            "status": "ok" if bulk.get("status") == "ok" else "error",
            "dry_run": False,
            "include_locked": True,
            "positions_total": len(all_positions),
            "to_exit_count": len(all_positions),
            "skipped_locked_count": 0,
            "success_count": success,
            "fail_count": fail,
            "bulk": bulk,
            "called_at": called_at,
        }

    results: list[dict[str, Any]] = []
    for pos in to_exit:
        results.append(_exit_single_position(pos))

    success_count = sum(1 for r in results if r.get("status") == "ok")
    fail_count = len(results) - success_count
    failures = [
        r for r in results if r.get("status") not in {"ok", "skipped"}
    ]

    _send_exit_completed(
        success_count=success_count,
        fail_count=fail_count,
        skipped_locked=len(skipped),
        failures=failures,
    )

    overall = "ok" if fail_count == 0 else "partial"
    return {
        "status": overall,
        "dry_run": False,
        "include_locked": False,
        "positions_total": len(all_positions),
        "to_exit_count": len(to_exit),
        "skipped_locked_count": len(skipped),
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results,
        "skipped_locked": skipped,
        "called_at": called_at,
    }


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Exit open Upstox positions. By default exits non-locked "
            "positions only. Pass --include-locked + --confirm "
            f"{CONFIRM_EXIT_LOCKED} to exit locked positions too."
        ),
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--include-locked", action="store_true",
        help="Also exit locked positions (requires --confirm).",
    )
    p.add_argument(
        "--confirm", default=None,
        help=f"Double-confirmation token. Must equal {CONFIRM_EXIT_LOCKED}.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    result = exit_all_positions(
        dry_run=args.dry_run,
        include_locked=args.include_locked,
        confirm=args.confirm,
    )
    status = result.get("status")
    if status in {"ok", "dry_run", "partial"}:
        print(f"{status.upper()}: {result}")
        return 0
    print(f"ERROR ({status}): {result.get('error')}")
    return 2 if status == "no_token" else 1


if __name__ == "__main__":
    sys.exit(main())