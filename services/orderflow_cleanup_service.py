"""Fail-closed cleanup of GTT exposure created by Orderflow.

Only GTT IDs supplied by the calling Orderflow instance are touched. Filled
entry orders are squared off by their exact Upstox order IDs; account-wide
exit_positions is deliberately never used here.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from core.config import ORDERFLOW_CLEANUP_ENABLED
from repositories.order_repository import OrderRepository, now_iso
from services.gtt_service import (
    GTTServiceError,
    cancel_gtt_order,
    get_gtt_order_details,
)
from services.order_service import OrderService, OrderServiceError

_TERMINAL = {"complete", "completed", "cancelled", "cancelled_partially", "expired", "rejected", "failed"}
_NO_ENTRY = {"failed", "expired", "cancelled", "inactive"}
_ACTIVE_UNTRIGGERED = {"scheduled", "pending"}


class OrderflowCleanupError(RuntimeError):
    def __init__(self, message: str, *, details: dict | None = None, status_code: int = 409):
        super().__init__(message)
        self.details = details or {}
        self.status_code = status_code


class OrderflowCleanupService:
    """Reconcile and remove exposure for explicitly supplied Orderflow GTT IDs."""

    def __init__(self, order_service: OrderService | None = None,
                 repository: OrderRepository | None = None) -> None:
        self.order_service = order_service or OrderService()
        self.repository = repository or OrderRepository()
        self._lock = threading.RLock()

    def _audit_gtt(self, gtt_id: str, gtt: dict) -> dict:
        entry = self.repository.find_order(gtt_id)
        if entry:
            return entry
        # Backward compatibility for GTTs placed before lifecycle auditing was
        # added. The ID came from Orderflow and Upstox details ground the fields.
        audit = {
            "local_id": str(uuid.uuid4()),
            "record_type": "GTT",
            "status": "TRACKED_FROM_BROKER",
            "order_id": gtt_id,
            "order_ids": [gtt_id],
            "gtt_order_ids": [gtt_id],
            "instrument_token": gtt.get("instrument_token"),
            "product": gtt.get("product") or "I",
            "transaction_type": "BUY",
            "quantity": int(gtt.get("quantity") or 0),
            "isolation_id": None,
            "created_time": now_iso(),
            "last_modified_time": now_iso(),
            "events": [{"event_type": "GTT_DISCOVERED_DURING_CLEANUP", "event_time": now_iso()}],
            "exit_orders_by_order_id": {},
        }
        self.repository.append_entry(audit, require_mongo=True)
        return audit

    def _update_audit(self, entry: dict, **updates) -> dict:
        updates["last_modified_time"] = now_iso()
        return self.repository.update_entry(entry["local_id"], updates, require_mongo=True)

    @staticmethod
    def _rule_status(rule: dict) -> str:
        return str(rule.get("status") or "").strip().lower()

    def _sync(self, order_id: str) -> dict:
        try:
            return self.order_service.sync_order_status(order_id)
        except Exception as exc:
            raise OrderflowCleanupError(
                f"Could not reconcile broker order {order_id}: {exc}",
                details={"order_id": order_id, "status": "UNKNOWN"},
            ) from None

    def _cancel_pending_order(self, order_id: str) -> dict:
        state = self._sync(order_id)
        if int(state.get("pending_quantity") or 0) > 0 and not state.get("is_terminal"):
            try:
                self.order_service.cancel_order(order_id, cleanup=True)
                state = self.order_service.wait_for_terminal_order_state(
                    order_id, timeout_seconds=8.0, poll_interval_seconds=0.5
                )
            except Exception as exc:
                raise OrderflowCleanupError(
                    f"Pending order {order_id} could not be cancelled and confirmed: {exc}",
                    details={"order_id": order_id, "status": "CANCEL_UNCONFIRMED"},
                ) from None
        if not state.get("is_terminal"):
            raise OrderflowCleanupError(
                f"Order {order_id} is still non-terminal after cancellation.",
                details={"order_id": order_id, "status": state.get("local_status")},
            )
        return state

    def _square_off_entry(self, entry_order_id: str, audit: dict) -> dict:
        entry_state = self._cancel_pending_order(entry_order_id)
        filled = int(entry_state.get("filled_quantity") or 0)
        if filled <= 0:
            return {"status": "NO_FILLED_ENTRY", "entry_order_id": entry_order_id}

        # A GTT's TARGET/STOPLOSS orders are linked to this exact ENTRY order.
        # Persist their IDs before asking exit_order to calculate any remainder.
        current = self.repository.find_order(entry_order_id) or audit
        exit_map = dict(current.get("exit_orders_by_order_id") or {})
        prior = list(exit_map.get(entry_order_id) or [])
        for candidate in current.get("gtt_exit_order_ids") or []:
            if candidate not in prior:
                prior.append(candidate)
        exit_map[entry_order_id] = prior
        self._update_audit(current, exit_orders_by_order_id=exit_map)

        last_exit = None
        for _ in range(3):
            current = self.repository.find_order(entry_order_id) or audit
            exit_ids = list((current.get("exit_orders_by_order_id") or {}).get(entry_order_id) or [])
            total_exited = 0
            for exit_id in exit_ids:
                state = self._cancel_pending_order(str(exit_id))
                total_exited += int(state.get("filled_quantity") or 0)
            if total_exited >= filled:
                return {
                    "status": "POSITION_SQUARED_OFF",
                    "entry_order_id": entry_order_id,
                    "filled_quantity": filled,
                    "exited_quantity": total_exited,
                }
            try:
                last_exit = self.order_service.exit_order(entry_order_id, cleanup=True)
            except Exception as exc:
                raise OrderflowCleanupError(
                    f"Could not square off filled Orderflow entry {entry_order_id}: {exc}",
                    details={"entry_order_id": entry_order_id, "filled_quantity": filled},
                ) from None
            if last_exit.get("status") == "already_exited":
                return {"status": "POSITION_ALREADY_EXITED", **last_exit}
            new_ids = list(last_exit.get("exit_order_ids") or [])
            if not new_ids:
                raise OrderflowCleanupError(
                    f"Square-off request for {entry_order_id} returned no exit order IDs.",
                    details={"entry_order_id": entry_order_id, "response": last_exit},
                )
            for exit_id in new_ids:
                state = self.order_service.wait_for_terminal_order_state(
                    str(exit_id), timeout_seconds=8.0, poll_interval_seconds=0.5
                )
                if not state.get("is_terminal"):
                    raise OrderflowCleanupError(
                        f"Square-off order {exit_id} is still pending; replacement is blocked.",
                        details={"entry_order_id": entry_order_id, "exit_order_id": exit_id,
                                 "status": state.get("local_status")},
                    )
        # Re-read every recorded exit after the bounded retry loop.
        current = self.repository.find_order(entry_order_id) or audit
        exit_ids = list((current.get("exit_orders_by_order_id") or {}).get(entry_order_id) or [])
        total_exited = sum(
            int((self._sync(str(exit_id)).get("filled_quantity") or 0))
            for exit_id in exit_ids
        )
        if total_exited < filled:
            raise OrderflowCleanupError(
                f"Orderflow position remains partially open for {entry_order_id}.",
                details={"entry_order_id": entry_order_id, "filled_quantity": filled,
                         "exited_quantity": total_exited, "last_exit": last_exit},
            )
        return {"status": "POSITION_SQUARED_OFF", "entry_order_id": entry_order_id,
                "filled_quantity": filled, "exited_quantity": total_exited}

    def cleanup(self, gtt_order_ids: list[str], *, isolation_id: str | None = None,
                source_order_id: str | None = None) -> dict[str, Any]:
        if not ORDERFLOW_CLEANUP_ENABLED:
            raise OrderflowCleanupError(
                "Orderflow broker cleanup is disabled. Set ORDERFLOW_CLEANUP_ENABLED=true.",
                status_code=403,
            )
        ids = list(dict.fromkeys(str(value).strip() for value in gtt_order_ids if str(value).strip()))
        if not ids or len(ids) > 20 or any(not value.startswith("GTT-") for value in ids):
            raise OrderflowCleanupError("Provide 1-20 valid GTT- order IDs.", status_code=400)

        results = []
        with self._lock:
            for gtt_id in ids:
                try:
                    details = get_gtt_order_details(gtt_id)
                    data = details.get("data")
                    gtt = data[0] if isinstance(data, list) and data else data
                    if not isinstance(gtt, dict):
                        raise OrderflowCleanupError(
                            f"Upstox returned no active details for {gtt_id}; completed GTTs cannot be queried. Reconcile manually.",
                            details={"gtt_order_id": gtt_id, "status": "UNKNOWN"},
                        )
                    audit = self._audit_gtt(gtt_id, gtt)
                    if isolation_id and not audit.get("isolation_id"):
                        self._update_audit(audit, isolation_id=isolation_id,
                                           source_order_id=source_order_id)
                    rules = [item for item in (gtt.get("rules") or []) if isinstance(item, dict)]
                    entry_rule = next((item for item in rules if str(item.get("strategy") or "").upper() == "ENTRY"), None)
                    if not entry_rule:
                        raise OrderflowCleanupError(
                            f"GTT {gtt_id} has no ENTRY rule; status cannot be reconciled.",
                            details={"gtt_order_id": gtt_id},
                        )
                    entry_status = self._rule_status(entry_rule)
                    if entry_status in _ACTIVE_UNTRIGGERED:
                        cancelled = cancel_gtt_order(gtt_id)
                        self._update_audit(audit, status="CANCELLED_UNTRIGGERED",
                                           last_gtt_response=cancelled)
                        results.append({"gtt_order_id": gtt_id, "status": "CANCELLED_UNTRIGGERED"})
                        continue
                    if entry_status in _NO_ENTRY:
                        self._update_audit(audit, status="NO_ENTRY_EXPOSURE",
                                           last_gtt_details=details)
                        results.append({"gtt_order_id": gtt_id, "status": "NO_ENTRY_EXPOSURE"})
                        continue

                    entry_order_id = str(entry_rule.get("order_id") or "").strip()
                    if not entry_order_id:
                        raise OrderflowCleanupError(
                            f"GTT {gtt_id} is {entry_status or 'unknown'} but has no generated ENTRY order ID.",
                            details={"gtt_order_id": gtt_id, "entry_status": entry_status},
                        )

                    # Make the exact broker-generated ENTRY order addressable
                    # through the GTT audit record before any cancel/exit action.
                    audit = self.repository.find_order(gtt_id) or audit
                    known_ids = list(audit.get("order_ids") or [])
                    if entry_order_id not in known_ids:
                        known_ids.append(entry_order_id)
                    exit_ids = []
                    for rule in rules:
                        if str(rule.get("strategy") or "").upper() == "ENTRY":
                            continue
                        order_id = str(rule.get("order_id") or "").strip()
                        if order_id and order_id not in exit_ids:
                            exit_ids.append(order_id)
                    for order_id in exit_ids:
                        if order_id not in known_ids:
                            known_ids.append(order_id)
                    exit_map = dict(audit.get("exit_orders_by_order_id") or {})
                    exit_map[entry_order_id] = list(dict.fromkeys(
                        list(exit_map.get(entry_order_id) or []) + exit_ids
                    ))
                    self._update_audit(
                        audit,
                        order_ids=known_ids,
                        instrument_token=gtt.get("instrument_token") or audit.get("instrument_token"),
                        product=gtt.get("product") or audit.get("product") or "I",
                        transaction_type=str(entry_rule.get("transaction_type") or "BUY").upper(),
                        quantity=int(gtt.get("quantity") or audit.get("quantity") or 0),
                        gtt_exit_order_ids=exit_ids,
                        exit_orders_by_order_id=exit_map,
                        last_gtt_details=details,
                    )

                    # A triggered ENTRY may still be pending. Cancel it, then
                    # square off any partial/full fill by its exact order ID.
                    outcome = self._square_off_entry(entry_order_id, audit)
                    self._update_audit(
                        audit,
                        status="CLEANED",
                        cleanup_result=outcome,
                        last_gtt_details=details,
                    )
                    results.append({"gtt_order_id": gtt_id, **outcome})
                except OrderflowCleanupError:
                    raise
                except (GTTServiceError, OrderServiceError, Exception) as exc:
                    raise OrderflowCleanupError(
                        f"Cleanup failed for {gtt_id}: {exc}",
                        details={"gtt_order_id": gtt_id, "status": "UNKNOWN"},
                    ) from None

        return {
            "success": True,
            "cleanup_complete": True,
            "isolation_id": isolation_id,
            "source_order_id": source_order_id,
            "results": results,
        }
