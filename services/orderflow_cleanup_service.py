"""Fail-closed cleanup of GTT exposure created by Orderflow.

Only GTT IDs supplied by the authenticated Orderflow caller are touched.
No account-wide exit_positions call is made by this service.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from core.config import ORDERFLOW_CLEANUP_ENABLED
from repositories.order_repository import OrderRepository, now_iso
from services.gtt_service import GTTServiceError, cancel_gtt_order, get_gtt_order_details
from services.order_service import OrderService, OrderServiceError

_TERMINAL = {"complete", "rejected", "cancelled", "cancelled_partially", "failed", "failed_partially", "expired"}
_NO_ENTRY = {"failed", "expired", "cancelled", "inactive"}


class OrderflowCleanupError(RuntimeError):
    def __init__(self, message: str, *, details: dict | None = None, status_code: int = 409):
        super().__init__(message)
        self.details = details or {}
        self.status_code = status_code


class OrderflowCleanupService:
    """Reconcile, cancel and square-off only explicitly supplied Orderflow GTTs."""

    def __init__(self, order_service: OrderService | None = None,
                 repository: OrderRepository | None = None) -> None:
        self.order_service = order_service or OrderService()
        self.repository = repository or OrderRepository()
        self._lock = threading.RLock()

    def _audit_gtt(self, gtt_id: str, gtt: dict) -> dict:
        entry = self.repository.find_order(gtt_id)
        if entry:
            return entry
        audit = {
            "local_id": f"GTT-{gtt_id}",
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

    def _ensure_entry_order_record(self, entry_order_id: str, gtt: dict,
                                   entry_rule: dict, gtt_id: str) -> dict:
        existing = self.repository.find_order(entry_order_id)
        if existing and existing.get("record_type") == "ORDER":
            return existing
        record = {
            "local_id": f"ORDER-{entry_order_id}",
            "record_type": "ORDER",
            "status": "PLACED",
            "order_id": entry_order_id,
            "order_ids": [entry_order_id],
            "instrument_token": gtt.get("instrument_token"),
            "trading_symbol": gtt.get("trading_symbol"),
            "quantity": int(gtt.get("quantity") or 0),
            "product": gtt.get("product") or "I",
            "transaction_type": str(entry_rule.get("transaction_type") or "BUY").upper(),
            "order_type": "MARKET",
            "source_gtt_order_id": gtt_id,
            "gtt_exit_order_ids": [],
            "exit_orders_by_order_id": {},
            "created_time": now_iso(),
            "last_modified_time": now_iso(),
            "events": [{"event_type": "GTT_ENTRY_ORDER_TRACKED", "event_time": now_iso(),
                        "gtt_order_id": gtt_id}],
        }
        self.repository.append_entry(record, require_mongo=True)
        return record

    def _square_off_entry(self, entry_order_id: str, order_record: dict) -> dict:
        entry_state = self._cancel_pending_order(entry_order_id)
        filled = int(entry_state.get("filled_quantity") or 0)
        if filled <= 0:
            return {"status": "NO_OPEN_POSITION", "entry_order_id": entry_order_id}

        # The GTT's TARGET/STOPLOSS rule orders are linked to this exact ENTRY.
        exit_map = dict(order_record.get("exit_orders_by_order_id") or {})
        prior = list(exit_map.get(entry_order_id) or [])
        for candidate in order_record.get("gtt_exit_order_ids") or []:
            if candidate not in prior:
                prior.append(candidate)
        exit_map[entry_order_id] = prior
        self.repository.update_entry(order_record["local_id"],
                                     {"exit_orders_by_order_id": exit_map,
                                      "last_modified_time": now_iso()},
                                     require_mongo=True)

        last_exit = None
        for _ in range(3):
            order_record = self.repository.find_order(entry_order_id) or order_record
            exit_ids = list((order_record.get("exit_orders_by_order_id") or {}).get(entry_order_id) or [])
            total_exited = 0
            for exit_id in exit_ids:
                exit_state = self._cancel_pending_order(str(exit_id))
                total_exited += int(exit_state.get("filled_quantity") or 0)
            if total_exited >= filled:
                return {"status": "POSITION_ALREADY_EXITED" if total_exited else "NO_OPEN_POSITION",
                        "entry_order_id": entry_order_id,
                        "filled_quantity": filled, "exited_quantity": total_exited}
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

        order_record = self.repository.find_order(entry_order_id) or order_record
        exit_ids = list((order_record.get("exit_orders_by_order_id") or {}).get(entry_order_id) or [])
        total_exited = sum(int(self._sync(str(value)).get("filled_quantity") or 0) for value in exit_ids)
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
                    gtt = data[0] if isinstance(data, list) and data else None
                    if not isinstance(gtt, dict):
                        raise OrderflowCleanupError(
                            f"Upstox returned no active details for {gtt_id}; completed GTTs are not queryable.",
                            details={"gtt_order_id": gtt_id, "status": "UNKNOWN"},
                        )
                    audit = self._audit_gtt(gtt_id, gtt)
                    if isolation_id and not audit.get("isolation_id"):
                        self._update_audit(audit, isolation_id=isolation_id,
                                           source_order_id=source_order_id)

                    rules = [item for item in (gtt.get("rules") or []) if isinstance(item, dict)]
                    entry_rule = next((item for item in rules
                                       if str(item.get("strategy") or "").upper() == "ENTRY"), None)
                    if not entry_rule:
                        raise OrderflowCleanupError(
                            f"GTT {gtt_id} has no ENTRY rule; lifecycle cannot be reconciled.",
                            details={"gtt_order_id": gtt_id},
                        )
                    entry_status = self._rule_status(entry_rule)
                    entry_order_id = str(entry_rule.get("order_id") or "").strip()

                    # SCHEDULED is the documented untriggered state. PENDING is
                    # intentionally not treated as safe-to-cancel at GTT level.
                    if entry_status == "scheduled" and not entry_order_id:
                        cancelled = cancel_gtt_order(gtt_id)
                        confirm = get_gtt_order_details(gtt_id)
                        confirm_data = confirm.get("data") or []
                        confirm_gtt = confirm_data[0] if confirm_data else {}
                        confirm_entry = next((item for item in (confirm_gtt.get("rules") or [])
                                               if str(item.get("strategy") or "").upper() == "ENTRY"), {})
                        if self._rule_status(confirm_entry) not in {"cancelled", "expired", "inactive"}:
                            raise OrderflowCleanupError(
                                f"GTT cancellation for {gtt_id} was accepted but not confirmed.",
                                details={"gtt_order_id": gtt_id, "status": "CANCEL_UNCONFIRMED",
                                         "cancel_response": cancelled},
                            )
                        self._update_audit(audit, status="CANCELLED_UNTRIGGERED",
                                           last_gtt_response=cancelled, last_gtt_details=confirm)
                        results.append({"gtt_order_id": gtt_id, "status": "CANCELLED_UNTRIGGERED"})
                        continue

                    if entry_status in _NO_ENTRY and not entry_order_id:
                        self._update_audit(audit, status="NO_ENTRY_EXPOSURE",
                                           last_gtt_details=details)
                        results.append({"gtt_order_id": gtt_id, "status": "NO_ENTRY_EXPOSURE"})
                        continue

                    if not entry_order_id:
                        raise OrderflowCleanupError(
                            f"GTT {gtt_id} is {entry_status or 'unknown'} but has no generated ENTRY order ID.",
                            details={"gtt_order_id": gtt_id, "entry_status": entry_status},
                        )

                    order_record = self._ensure_entry_order_record(entry_order_id, gtt, entry_rule, gtt_id)
                    exit_ids = []
                    for rule in rules:
                        if str(rule.get("strategy") or "").upper() == "ENTRY":
                            continue
                        order_id = str(rule.get("order_id") or "").strip()
                        if order_id and order_id not in exit_ids:
                            exit_ids.append(order_id)
                    order_record = self.repository.find_order(entry_order_id) or order_record
                    self.repository.update_entry(
                        order_record["local_id"],
                        {"gtt_exit_order_ids": exit_ids,
                         "source_gtt_order_id": gtt_id,
                         "last_gtt_details": details,
                         "last_modified_time": now_iso()},
                        require_mongo=True,
                    )
                    outcome = self._square_off_entry(entry_order_id, order_record)
                    self._update_audit(audit, status="CLEANED", cleanup_result=outcome,
                                       last_gtt_details=details)
                    results.append({"gtt_order_id": gtt_id, **outcome})
                except OrderflowCleanupError:
                    raise
                except Exception as exc:
                    raise OrderflowCleanupError(
                        f"Cleanup failed for {gtt_id}: {exc}",
                        details={"gtt_order_id": gtt_id, "status": "UNKNOWN"},
                    ) from None

        return {"success": True, "cleanup_complete": True,
                "isolation_id": isolation_id, "source_order_id": source_order_id,
                "results": results}
