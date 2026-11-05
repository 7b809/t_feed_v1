# services/sandbox_execution_service.py
"""Live sandbox execution engine.

Consumes transitions emitted by dummy_order_service and mirrors them as real
broker orders, but ONLY on the scheduled (live) tick. Startup catch-up
transitions (reason="startup") are always ignored — the determinism contract
forbids replaying them as real orders.

v1 design decisions (2026-11-04):
- Entry:   place_order(MARKET, product="D", transaction_type="BUY")
- Exit:    exit_all_positions(dry_run=False) — closes every open position.
           Because exit_all cannot target one key, a single-position lock is
           enforced: only one sandbox-owned position may exist at a time.
- Bug A:   STOP_HIT is detected on the 30 s dummy tick, so the sandbox stop is
           a 30 s-latency market exit, not a resting SL order. Accepted for v1.
           Latency + slippage fields are recorded on every entry/exit so we can
           decide later whether to add a resting SL-M order.
- Bug B:   The sandbox owns its own EOD at SANDBOX_EOD_HOUR:SANDBOX_EOD_MINUTE
           (default 15:15), BEFORE the broker's ~15:15–15:25 auto-square-off
           and BEFORE the dummy's 15:35 accounting EOD_CLOSE. The dummy 15:35
           EOD_CLOSE is therefore a no-op for sandbox state (see _handle_eod_close).

Telegram notifications (HTML)
-----------------------------
Only the key lifecycle events are reported — not every transition:
  - ENTRY started / filled / failed
  - EXIT started / exited / failed
  - PAUSED (token expiry etc.)
  - exit_stuck
  - force_eod_exit summary
  - reconcile_startup summary
  - unhandled exception inside on_dummy_transition
  - registration failure
All sends are best-effort and never affect trading or the ledger.

Companion file required:
- upstox_services/get_order_details.py -> get_order_details(broker_order_id) -> dict
  Must return at least: {"status": str, "filled_quantity": int, "average_price": float}
  Used to confirm the entry fill before transitioning to ENTERED.

Companion dummy_order_service change required:
- dummy_order_service must expose set_transition_callback(fn) where
  fn(transition_type, order_record, reason) is called AFTER the dummy order
  is persisted to data/dummy_orders.json. The callback must be invoked for:
  "CREATED", "STAGE_2_MILESTONE", "STOP_HIT", "STAGE_2_RESISTANCE_EXIT",
  "NEXT_15_3_RESISTANCE_EXIT", "EOD_CLOSE". It must never raise into run_once.
"""

from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from services.order_runtime_service import order_runtime_service
from services.sr_backfill_service import sr_backfill_service
from services.telegram_service import telegram_service
from upstox_services.exit_all_positions import exit_all_positions
from upstox_services.get_order_details import get_order_details
from upstox_services.place_order import place_order

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Env config
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip())
    except Exception:
        return default


SANDBOX_EXECUTION_ENABLED       = _env_bool("SANDBOX_EXECUTION_ENABLED", False)
SANDBOX_EXECUTION_MODE          = str(os.getenv("SANDBOX_EXECUTION_MODE", "observe")).strip().lower()  # "observe" | "live"
SANDBOX_LOTS                    = _env_int("SANDBOX_LOTS", 1)
SANDBOX_EOD_HOUR                = _env_int("SANDBOX_EOD_HOUR", 15)
SANDBOX_EOD_MINUTE              = _env_int("SANDBOX_EOD_MINUTE", 15)
SANDBOX_LEDGER_PATH             = Path(os.getenv("SANDBOX_LEDGER_PATH", "data/sandbox_orders.json"))
SANDBOX_EXIT_RETRY_ATTEMPTS     = _env_int("SANDBOX_EXIT_RETRY_ATTEMPTS", 3)
SANDBOX_EXIT_RETRY_BACKOFF_S    = _env_float("SANDBOX_EXIT_RETRY_BACKOFF_SECONDS", 1.5)
SANDBOX_FILL_TIMEOUT_S          = _env_float("SANDBOX_FILL_TIMEOUT_SECONDS", 15.0)
SANDBOX_FILL_POLL_S             = _env_float("SANDBOX_FILL_POLL_SECONDS", 1.0)
SANDBOX_MARKET_OPEN             = dtime(9, 15)
# Execution window for normal-flow transitions. EOD is handled by force_eod_exit()
# via the scheduler at SANDBOX_EOD_HOUR:SANDBOX_EOD_MINUTE (default 15:15) and
# bypasses this window by design (safety: always try to flatten at EOD).
SANDBOX_MARKET_CLOSE            = dtime(15, 15)


# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

ST_PENDING_ENTRY = "PENDING_ENTRY"
ST_ENTERED       = "ENTERED"
ST_EXIT_PENDING  = "EXIT_PENDING"
ST_EXITED        = "EXITED"
ST_ENTRY_FAILED  = "ENTRY_FAILED"
ST_EXIT_FAILED   = "EXIT_FAILED"
ST_SKIPPED       = "SKIPPED"

EXIT_TRANSITIONS = {
    "STOP_HIT",
    "STAGE_2_RESISTANCE_EXIT",
    "NEXT_15_3_RESISTANCE_EXIT",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).isoformat()


def _now() -> datetime:
    return datetime.now(IST)


# ---------------------------------------------------------------------------
# Telegram helpers — best-effort, never raise, HTML formatted.
# ---------------------------------------------------------------------------

def _tg_escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _format_response_for_tg(response_summary: Any) -> str:
    """Render a sanitised response dict as Telegram HTML bullet lines."""
    if not isinstance(response_summary, dict) or not response_summary:
        return f"<i>{_tg_escape(response_summary)}</i>"
    lines: list[str] = []
    for k, v in response_summary.items():
        if isinstance(v, dict):
            inner = ", ".join(
                f"{_tg_escape(ik)}={_tg_escape(iv)}" for ik, iv in v.items()
            )
            lines.append(f"• <b>{_tg_escape(k)}</b>: {{{inner}}}")
        elif isinstance(v, (list, tuple)):
            inner = ", ".join(_tg_escape(x) for x in v)
            lines.append(f"• <b>{_tg_escape(k)}</b>: [{inner}]")
        else:
            lines.append(f"• <b>{_tg_escape(k)}</b>: {_tg_escape(v)}")
    return "\n".join(lines) if lines else "<i>(empty)</i>"


def _tg(title: str, body: str, level: str = "INFO") -> None:
    """Best-effort Telegram send. Never raises."""
    try:
        telegram_service.send_message(title, body, level=level)
    except Exception:
        logger.exception("[sandbox_exec] telegram notify failed title=%s", title)


def _tg_entry_started(*, oid: str, instrument_key: str, bias: str,
                      lots: int, dummy_entry: float, mode: str) -> None:
    _tg(
        "🟡 Sandbox · ENTRY started",
        (
            f"<b>Order</b>: <code>{_tg_escape(oid)}</code>\n"
            f"<b>Instrument</b>: <code>{_tg_escape(instrument_key)}</code>\n"
            f"<b>Bias</b>: {_tg_escape(bias or '-')}\n"
            f"<b>Lots</b>: {_tg_escape(lots)}\n"
            f"<b>Dummy entry</b>: {_tg_escape(dummy_entry)}\n"
            f"<b>Mode</b>: {_tg_escape(mode)}"
        ),
        level="INFO",
    )


def _tg_entry_filled(*, oid: str, broker_order_id: str, instrument_key: str,
                     fill_price: float, dummy_entry: float,
                     slippage_pct: Any, latency_ms: Any) -> None:
    _tg(
        "✅ Sandbox · ENTRY filled",
        (
            f"<b>Order</b>: <code>{_tg_escape(oid)}</code>\n"
            f"<b>Broker</b>: <code>{_tg_escape(broker_order_id)}</code>\n"
            f"<b>Instrument</b>: <code>{_tg_escape(instrument_key)}</code>\n"
            f"<b>Fill</b>: {_tg_escape(fill_price)}\n"
            f"<b>Dummy</b>: {_tg_escape(dummy_entry)}\n"
            f"<b>Slippage</b>: {_tg_escape(slippage_pct)}%\n"
            f"<b>Detect→fill</b>: {_tg_escape(latency_ms)} ms"
        ),
        level="SUCCESS",
    )


def _tg_entry_failed(*, oid: str, error: str) -> None:
    _tg(
        "❌ Sandbox · ENTRY failed",
        (
            f"<b>Order</b>: <code>{_tg_escape(oid)}</code>\n"
            f"<b>Error</b>:\n<pre>{_tg_escape(error)}</pre>"
        ),
        level="ERROR",
    )


def _tg_exit_started(*, oid: str, reason: str, instrument_key: str) -> None:
    _tg(
        "🟠 Sandbox · EXIT started",
        (
            f"<b>Order</b>: <code>{_tg_escape(oid)}</code>\n"
            f"<b>Reason</b>: {_tg_escape(reason)}\n"
            f"<b>Instrument</b>: <code>{_tg_escape(instrument_key)}</code>"
        ),
        level="WARNING",
    )


def _tg_exited(*, oid: str, reason: str, exit_price: Any, dummy_exit: Any,
               latency_ms: Any, external: bool = False) -> None:
    tag = " (external)" if external else ""
    _tg(
        f"✅ Sandbox · EXITED{tag}",
        (
            f"<b>Order</b>: <code>{_tg_escape(oid)}</code>\n"
            f"<b>Reason</b>: {_tg_escape(reason)}\n"
            f"<b>Exit</b>: {_tg_escape(exit_price)}\n"
            f"<b>Dummy</b>: {_tg_escape(dummy_exit)}\n"
            f"<b>Detect→exit</b>: {_tg_escape(latency_ms)} ms"
        ),
        level="SUCCESS",
    )


def _tg_exit_failed(*, oid: str, error: str) -> None:
    _tg(
        "❌ Sandbox · EXIT failed",
        (
            f"<b>Order</b>: <code>{_tg_escape(oid)}</code>\n"
            f"<b>Error</b>:\n<pre>{_tg_escape(error)}</pre>\n"
            f"<i>sandbox_exit_stuck=true — new entries blocked</i>"
        ),
        level="ERROR",
    )


def _tg_paused(*, reason: str) -> None:
    _tg(
        "⏸ Sandbox · PAUSED",
        f"<b>Reason</b>: {_tg_escape(reason)}\n<i>New transitions ignored until resume.</i>",
        level="ERROR",
    )


def _tg_force_eod_exit(*, processed: int) -> None:
    _tg(
        "⏰ Sandbox · EOD force exit",
        f"<b>Processed</b>: {_tg_escape(processed)} open position(s)",
        level="WARNING",
    )


def _tg_reconcile_summary(*, summary: dict) -> None:
    _tg(
        "🔄 Sandbox · Startup reconcile",
        _format_response_for_tg(summary),
        level="INFO",
    )


def _tg_unhandled(*, ttype: str, oid: str, error: str) -> None:
    _tg(
        "💥 Sandbox · Unhandled transition error",
        (
            f"<b>Transition</b>: {_tg_escape(ttype)}\n"
            f"<b>Order</b>: <code>{_tg_escape(oid)}</code>\n"
            f"<b>Error</b>:\n<pre>{_tg_escape(error)}</pre>"
        ),
        level="ERROR",
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class SandboxExecutionService:
    """Mirrors dummy order transitions into real broker orders (scheduled flow only)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ledger: dict[str, dict] = {}
        self._loaded = False
        self._paused = False
        self._pause_reason: Optional[str] = None
        self._exit_stuck = False
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------ state

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if SANDBOX_LEDGER_PATH.exists():
                with SANDBOX_LEDGER_PATH.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("orders"), dict):
                    self._ledger = data["orders"]
                elif isinstance(data, dict):
                    self._ledger = data
                else:
                    self._ledger = {}
        except Exception as e:
            logger.warning(f"[sandbox_exec] ledger load failed ({SANDBOX_LEDGER_PATH}): {e}")
            self._ledger = {}

    def _save(self) -> None:
        try:
            SANDBOX_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = SANDBOX_LEDGER_PATH.with_suffix(SANDBOX_LEDGER_PATH.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(
                    {"orders": self._ledger, "updated_at": _iso(_now())},
                    f,
                    default=str,
                    indent=2,
                )
            tmp.replace(SANDBOX_LEDGER_PATH)
        except Exception as e:
            logger.error(f"[sandbox_exec] ledger save failed ({SANDBOX_LEDGER_PATH}): {e}")

    def _pause(self, reason: str) -> None:
        first = not self._paused
        self._paused = True
        self._pause_reason = reason
        self._last_error = f"paused:{reason}"
        logger.warning(f"[sandbox_exec] PAUSED reason={reason}")
        if first:
            _tg_paused(reason=reason)

    # ------------------------------------------------------------------- gate

    def _gate(self, reason: str) -> Optional[str]:
        """Fixed-order gate chain. Returns None on pass, else a skip reason string."""
        if reason != "scheduled":
            return f"reason_not_scheduled:{reason}"
        if not SANDBOX_EXECUTION_ENABLED:
            return "execution_disabled"
        if self._paused:
            return f"paused:{self._pause_reason}"
        if self._exit_stuck:
            return "exit_stuck"
        try:
            rt = order_runtime_service.status() or {}
            if not bool(rt.get("enabled", True)):
                return "runtime_disabled"
        except Exception as e:
            return f"runtime_status_error:{e}"
        try:
            probe = getattr(sr_backfill_service, "is_ready_for_today", None)
            if probe is None:
                probe = getattr(sr_backfill_service, "is_ready", None)
            if probe is None:
                return "sr_readiness_probe_missing"
            if not bool(probe()):
                return "sr_not_ready"
        except Exception as e:
            return f"sr_ready_error:{e}"
        now_t = _now().time()
        if not (SANDBOX_MARKET_OPEN <= now_t <= SANDBOX_MARKET_CLOSE):
            return f"outside_market_hours:{now_t.strftime('%H:%M:%S')}"
        return None

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _order_id(order_record: dict) -> str:
        return str(
            order_record.get("order_id")
            or order_record.get("id")
            or order_record.get("key")
            or ""
        ).strip()

    @staticmethod
    def _trading_day(order_id: str) -> Optional[str]:
        # order_id format: NSE_FO|39957|2026-11-04T09:25:00+05:30
        for part in str(order_id).split("|"):
            if len(part) >= 10 and part[4] == "-" and part[7] == "-":
                return part[:10]
        return None

    def _record_time(self, order_record: dict) -> str:
        for k in ("transition_at", "updated_at", "closed_at", "created_at"):
            v = order_record.get(k)
            if v:
                return str(v)
        return _iso(_now())

    @staticmethod
    def _dummy_exit_price(order_record: dict, transition_type: str) -> Optional[float]:
        for k in ("close_price", "exit_price", "price"):
            v = order_record.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    pass
        if transition_type == "STOP_HIT":
            try:
                return float(order_record.get("stop") or 0.0) or None
            except Exception:
                return None
        return None

    def _find_open_position(self, exclude_order_id: Optional[str] = None) -> Optional[dict]:
        today = _now().strftime("%Y-%m-%d")
        for rec in self._ledger.values():
            oid = rec.get("order_id") or ""
            if exclude_order_id and oid == exclude_order_id:
                continue
            if self._trading_day(oid) not in {today, None}:
                continue
            es = rec.get("entry", {}).get("state")
            xs = rec.get("exit", {}).get("state")
            if es in {ST_PENDING_ENTRY, ST_ENTERED} and xs not in {ST_EXITED, ST_EXIT_FAILED}:
                return rec
        return None

    def _record_skip(self, order_id: str, order_record: dict, skip_reason: str) -> None:
        rec = self._ledger.get(order_id) or {}
        rec.setdefault("order_id", order_id)
        rec.setdefault("instrument_key", str(order_record.get("instrument_key") or "").strip())
        rec.setdefault("bias", str(order_record.get("bias") or "").upper())
        rec.setdefault("trading_day", self._trading_day(order_id) or _now().strftime("%Y-%m-%d"))
        rec.setdefault("entry", {"state": ST_SKIPPED})
        rec.setdefault("exit", {"state": None, "reason": None})
        rec["entry"]["state"] = ST_SKIPPED
        rec["entry"]["skip_reason"] = skip_reason
        rec["updated_at"] = _iso(_now())
        self._ledger[order_id] = rec
        self._save()

    @staticmethod
    def _position_open(pre: Optional[dict], instrument_key: str) -> Optional[bool]:
        """True=open, False=flat, None=unknown."""
        if not isinstance(pre, dict):
            return None
        positions = pre.get("positions") or pre.get("data") or []
        if isinstance(positions, dict):
            positions = positions.get("positions") or []
        if not isinstance(positions, list):
            return None
        for p in positions:
            if not isinstance(p, dict):
                continue
            ikey = str(p.get("instrument_key") or p.get("instrument_token") or "").strip()
            qty = p.get("quantity", p.get("net_quantity", 0))
            try:
                qty = int(qty)
            except Exception:
                continue
            if ikey == instrument_key and qty != 0:
                return True
        return False

    @staticmethod
    def _exit_result_ok(result: Any, instrument_key: str) -> bool:
        if not isinstance(result, dict):
            return False
        status = str(result.get("status") or "").lower()
        if status and status not in {"ok", "success"}:
            return False
        closed = result.get("closed") or result.get("positions") or result.get("results") or []
        if isinstance(closed, list) and closed:
            for c in closed:
                if not isinstance(c, dict):
                    continue
                ikey = str(c.get("instrument_key") or c.get("instrument_token") or "").strip()
                if ikey == instrument_key:
                    return True
            # Response contained positions but not ours: assume broker did not close us.
            return False
        # No per-position breakdown provided; trust status==ok but log at debug.
        logger.debug(f"[sandbox_exec] exit result ok with no per-position detail for {instrument_key}")
        return True

    @staticmethod
    def _extract_exit_price(result: Any, instrument_key: str) -> Optional[float]:
        if not isinstance(result, dict):
            return None
        closed = result.get("closed") or result.get("positions") or result.get("results") or []
        if not isinstance(closed, list):
            return None
        for c in closed:
            if not isinstance(c, dict):
                continue
            ikey = str(c.get("instrument_key") or c.get("instrument_token") or "").strip()
            if ikey != instrument_key:
                continue
            for k in ("average_price", "avg_price", "exit_price", "price", "fill_price"):
                v = c.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except Exception:
                        pass
        return None

    def _wait_for_fill(self, broker_order_id: str, timeout_seconds: float) -> Optional[dict]:
        deadline = time.time() + max(1.0, timeout_seconds)
        last: Optional[dict] = None
        while time.time() < deadline:
            try:
                d = get_order_details(broker_order_id)
            except Exception as e:
                logger.debug(f"[sandbox_exec] get_order_details({broker_order_id}) error: {e}")
                d = None
            if isinstance(d, dict):
                last = d
                status = str(d.get("status") or "").lower()
                try:
                    filled = int(d.get("filled_quantity") or 0)
                except Exception:
                    filled = 0
                if status in {"complete", "filled", "traded"} and filled > 0:
                    return d
                if status in {"rejected", "cancelled", "canceled", "failed"}:
                    return d
            time.sleep(SANDBOX_FILL_POLL_S)
        return last

    # ---------------------------------------------------------- public: main

    def on_dummy_transition(self, transition_type: str, order_record: dict, reason: str) -> None:
        """Callback invoked by dummy_order_service AFTER persistence. Must never raise."""
        ttype = str(transition_type or "").upper()
        oid = ""
        try:
            oid = self._order_id(order_record)
            if not oid:
                logger.warning(f"[sandbox_exec] {ttype} transition with no order_id — ignored")
                return

            with self._lock:
                self._load()

                skip = self._gate(reason)
                if skip:
                    logger.info(f"[sandbox_exec] skip key={oid}::{ttype} reason={skip}")
                    return

                if ttype == "CREATED":
                    self._handle_entry(order_record)
                elif ttype in EXIT_TRANSITIONS:
                    self._handle_exit(ttype, order_record)
                elif ttype == "EOD_CLOSE":
                    self._handle_eod_close(order_record)
                elif ttype == "STAGE_2_MILESTONE":
                    self._handle_milestone(order_record)
                else:
                    logger.debug(f"[sandbox_exec] unhandled transition {ttype} for {oid}")
        except Exception as e:
            logger.error(f"[sandbox_exec] on_dummy_transition crashed: {e}", exc_info=True)
            _tg_unhandled(ttype=ttype, oid=oid or "-", error=f"{type(e).__name__}: {e}")

    # --------------------------------------------------------------- entry

    def _handle_entry(self, order_record: dict) -> None:
        oid = self._order_id(order_record)
        existing = self._ledger.get(oid)
        if existing and existing.get("entry", {}).get("state") in {ST_PENDING_ENTRY, ST_ENTERED}:
            logger.debug(f"[sandbox_exec] entry already handled for {oid}")
            return

        locked = self._find_open_position(exclude_order_id=oid)
        if locked:
            self._record_skip(oid, order_record, "single_position_lock_busy")
            logger.warning(
                f"[sandbox_exec] single_position_lock_busy held_by={locked.get('order_id')} "
                f"skipping entry for {oid}"
            )
            return

        instrument_key = str(order_record.get("instrument_key") or "").strip()
        if not instrument_key:
            logger.warning(f"[sandbox_exec] CREATED without instrument_key for {oid}")
            return

        dummy_entry = float(order_record.get("entry") or 0.0)
        dummy_at = self._record_time(order_record)

        record = {
            "order_id": oid,
            "trading_day": self._trading_day(oid) or _now().strftime("%Y-%m-%d"),
            "instrument_key": instrument_key,
            "bias": str(order_record.get("bias") or "").upper(),
            "lots": SANDBOX_LOTS,
            "dummy_entry_price": dummy_entry,
            "dummy_entry_transition_at": dummy_at,
            "dummy_exit_price": None,
            "dummy_exit_transition_at": None,
            "dummy_exit_reason": None,
            "entry": {
                "state": ST_PENDING_ENTRY,
                "broker_order_id": None,
                "sandbox_requested_at": None,
                "sandbox_responded_at": None,
                "sandbox_fill_at": None,
                "sandbox_fill_price": None,
                "fill_slippage_pct": None,
                "detection_to_fill_ms": None,
                "error": None,
                "attempts": 0,
            },
            "exit": {"state": None, "reason": None},
            "milestones": [],
            "updated_at": _iso(_now()),
        }
        self._ledger[oid] = record
        # PENDING written *before* the broker call so a restart sees intent.
        self._save()

        if SANDBOX_EXECUTION_MODE != "live":
            entry = record["entry"]
            entry["state"] = ST_ENTERED
            entry["sandbox_fill_at"] = _iso(_now())
            entry["sandbox_fill_price"] = dummy_entry
            entry["detection_to_fill_ms"] = 0
            entry["fill_slippage_pct"] = 0.0
            entry["broker_order_id"] = "OBSERVE"
            record["updated_at"] = _iso(_now())
            self._save()
            logger.info(
                f"[sandbox_exec] OBSERVE ENTRY key={oid} instrument={instrument_key} "
                f"dummy_entry={dummy_entry}"
            )
            _tg_entry_started(
                oid=oid, instrument_key=instrument_key,
                bias=str(order_record.get("bias") or ""),
                lots=SANDBOX_LOTS, dummy_entry=dummy_entry, mode="observe",
            )
            return

        requested = _now()
        entry = record["entry"]
        entry["sandbox_requested_at"] = _iso(requested)
        entry["attempts"] += 1
        self._save()

        _tg_entry_started(
            oid=oid, instrument_key=instrument_key,
            bias=str(order_record.get("bias") or ""),
            lots=SANDBOX_LOTS, dummy_entry=dummy_entry, mode="live",
        )

        try:
            result = place_order(
                instrument_key,
                lots=SANDBOX_LOTS,
                transaction_type="BUY",
                product="D",
                order_type="MARKET",
                price=0.0,
                trigger_price=0.0,
                validity="DAY",
                tag="sandbox",
            )
        except Exception as e:
            self._entry_failed(record, f"place_order_exception:{e}")
            return

        responded = _now()
        entry["sandbox_responded_at"] = _iso(responded)

        status = str(result.get("status") or "").lower() if isinstance(result, dict) else ""
        if status == "no_token":
            self._entry_failed(record, "place_order_no_token")
            self._pause("token_expired")
            return
        if status != "ok":
            self._entry_failed(record, f"place_order_status:{status or 'unknown'} detail={result}")
            return

        broker_order_id = ""
        if isinstance(result, dict):
            broker_order_id = str(
                result.get("order_id")
                or (result.get("data") or {}).get("order_id")
                or ""
            ).strip()
        if not broker_order_id:
            self._entry_failed(record, "place_order_missing_order_id")
            return
        entry["broker_order_id"] = broker_order_id
        self._save()

        fill = self._wait_for_fill(broker_order_id, timeout_seconds=SANDBOX_FILL_TIMEOUT_S)
        try:
            filled_qty = int((fill or {}).get("filled_quantity") or 0)
        except Exception:
            filled_qty = 0
        if not fill or filled_qty <= 0:
            self._entry_failed(record, f"entry_not_filled broker={broker_order_id} detail={fill}")
            return

        fill_at = _now()
        fill_price = float(fill.get("average_price") or 0.0)
        entry["state"] = ST_ENTERED
        entry["sandbox_fill_at"] = _iso(fill_at)
        entry["sandbox_fill_price"] = fill_price
        if dummy_entry > 0:
            entry["fill_slippage_pct"] = round(((fill_price - dummy_entry) / dummy_entry) * 100.0, 4)
        entry["detection_to_fill_ms"] = int((fill_at - requested).total_seconds() * 1000)
        record["updated_at"] = _iso(_now())
        self._save()

        logger.info(
            f"[sandbox_exec] ENTERED key={oid} broker={broker_order_id} instrument={instrument_key} "
            f"fill={fill_price:.4f} dummy={dummy_entry:.4f} slip={entry['fill_slippage_pct']}% "
            f"detect_to_fill={entry['detection_to_fill_ms']}ms"
        )
        _tg_entry_filled(
            oid=oid, broker_order_id=broker_order_id, instrument_key=instrument_key,
            fill_price=fill_price, dummy_entry=dummy_entry,
            slippage_pct=entry.get("fill_slippage_pct"),
            latency_ms=entry.get("detection_to_fill_ms"),
        )

    def _entry_failed(self, record: dict, error: str) -> None:
        record["entry"]["state"] = ST_ENTRY_FAILED
        record["entry"]["error"] = str(error)
        record["updated_at"] = _iso(_now())
        self._last_error = f"{record['order_id']}:entry:{error}"
        self._save()
        logger.warning(f"[sandbox_exec] ENTRY_FAILED key={record['order_id']} err={error}")
        _tg_entry_failed(oid=str(record["order_id"]), error=str(error))

    # ---------------------------------------------------------------- exit

    def _handle_exit(self, transition_type: str, order_record: dict) -> None:
        oid = self._order_id(order_record)
        record = self._ledger.get(oid)
        if not record:
            logger.info(f"[sandbox_exec] exit transition {transition_type} for untracked order {oid} — no-op")
            return

        entry_state = record.get("entry", {}).get("state")
        if entry_state in {ST_ENTRY_FAILED, ST_PENDING_ENTRY, ST_SKIPPED, None}:
            logger.info(f"[sandbox_exec] exit for {oid} but entry state={entry_state} — no-op")
            return

        exit_state = record.get("exit", {}).get("state")
        if exit_state in {ST_EXIT_PENDING, ST_EXITED}:
            logger.debug(f"[sandbox_exec] exit already handled for {oid} state={exit_state}")
            return

        dummy_exit = self._dummy_exit_price(order_record, transition_type)
        record["dummy_exit_price"] = dummy_exit
        record["dummy_exit_transition_at"] = self._record_time(order_record)
        record["dummy_exit_reason"] = transition_type
        record["exit"]["reason"] = transition_type
        record["exit"]["state"] = ST_EXIT_PENDING
        record["exit"].setdefault("attempts", 0)
        record["updated_at"] = _iso(_now())
        self._save()

        if SANDBOX_EXECUTION_MODE != "live":
            record["exit"]["state"] = ST_EXITED
            record["exit"]["sandbox_exited_at"] = _iso(_now())
            record["exit"]["sandbox_exit_price"] = dummy_exit
            record["updated_at"] = _iso(_now())
            self._save()
            logger.info(
                f"[sandbox_exec] OBSERVE EXIT key={oid} reason={transition_type} dummy_exit={dummy_exit}"
            )
            _tg_exit_started(
                oid=oid, reason=transition_type,
                instrument_key=str(record.get("instrument_key") or ""),
            )
            _tg_exited(
                oid=oid, reason=transition_type, exit_price=dummy_exit,
                dummy_exit=dummy_exit, latency_ms=0,
            )
            return

        self._execute_exit(record, transition_type)

    def _execute_exit(self, record: dict, transition_type: str) -> None:
        oid = record["order_id"]
        instrument_key = record["instrument_key"]
        requested = _now()
        record["exit"]["sandbox_requested_at"] = _iso(requested)
        record["exit"]["attempts"] = int(record["exit"].get("attempts") or 0) + 1
        record["updated_at"] = _iso(_now())
        self._save()

        _tg_exit_started(oid=oid, reason=transition_type, instrument_key=instrument_key)

        # Pre-check: is the position still open?
        try:
            pre = exit_all_positions(dry_run=True)
        except Exception as e:
            logger.warning(f"[sandbox_exec] exit dry_run failed for {oid}: {e}")
            pre = None
        still_open = self._position_open(pre, instrument_key)
        if still_open is False:
            record["exit"]["state"] = ST_EXITED
            record["exit"]["status_detail"] = "EXITED(external)"
            record["exit"]["sandbox_exited_at"] = _iso(_now())
            record["exit"]["sandbox_exit_price"] = None
            record["updated_at"] = _iso(_now())
            self._save()
            logger.info(f"[sandbox_exec] EXITED(external) key={oid} reason={transition_type}")
            _tg_exited(
                oid=oid, reason=transition_type, exit_price=None,
                dummy_exit=record.get("dummy_exit_price"),
                latency_ms=None, external=True,
            )
            return

        last_err: Optional[str] = None
        for attempt in range(1, SANDBOX_EXIT_RETRY_ATTEMPTS + 1):
            try:
                result = exit_all_positions(dry_run=False)
            except Exception as e:
                last_err = f"exception:{e}"
                logger.warning(f"[sandbox_exec] exit attempt {attempt}/{SANDBOX_EXIT_RETRY_ATTEMPTS} exception for {oid}: {e}")
            else:
                if self._exit_result_ok(result, instrument_key):
                    responded = _now()
                    exit_price = self._extract_exit_price(result, instrument_key)
                    if exit_price is None:
                        exit_price = record.get("dummy_exit_price")
                    record["exit"]["state"] = ST_EXITED
                    record["exit"]["sandbox_responded_at"] = _iso(responded)
                    record["exit"]["sandbox_exited_at"] = _iso(responded)
                    record["exit"]["sandbox_exit_price"] = exit_price
                    dummy_exit = record.get("dummy_exit_price")
                    if dummy_exit and exit_price:
                        record["exit"]["exit_slippage_pct"] = round(
                            ((exit_price - dummy_exit) / dummy_exit) * 100.0, 4
                        )
                    try:
                        req_dt = datetime.fromisoformat(record["exit"]["sandbox_requested_at"])
                        record["exit"]["detection_to_exit_ms"] = int(
                            (responded - req_dt).total_seconds() * 1000
                        )
                    except Exception:
                        record["exit"]["detection_to_exit_ms"] = None
                    record["updated_at"] = _iso(_now())
                    self._save()
                    logger.info(
                        f"[sandbox_exec] EXITED key={oid} reason={transition_type} "
                        f"exit={exit_price} dummy={dummy_exit} "
                        f"detect_to_exit={record['exit'].get('detection_to_exit_ms')}ms"
                    )
                    _tg_exited(
                        oid=oid, reason=transition_type, exit_price=exit_price,
                        dummy_exit=dummy_exit,
                        latency_ms=record["exit"].get("detection_to_exit_ms"),
                    )
                    return
                else:
                    last_err = f"result_not_ok:{result}"
                    logger.warning(
                        f"[sandbox_exec] exit attempt {attempt}/{SANDBOX_EXIT_RETRY_ATTEMPTS} "
                        f"result not ok for {oid}: {result}"
                    )
            if attempt < SANDBOX_EXIT_RETRY_ATTEMPTS:
                time.sleep(SANDBOX_EXIT_RETRY_BACKOFF_S * attempt)

        record["exit"]["state"] = ST_EXIT_FAILED
        record["exit"]["error"] = str(last_err)
        record["updated_at"] = _iso(_now())
        self._exit_stuck = True
        self._last_error = f"{oid}:exit:{last_err}"
        self._save()
        logger.error(
            f"[sandbox_exec] EXIT_FAILED key={oid} err={last_err} "
            f"— sandbox_exit_stuck=true, blocking new entries"
        )
        _tg_exit_failed(oid=oid, error=str(last_err))

    # -------------------------------------------------------------- specials

    def _handle_eod_close(self, order_record: dict) -> None:
        oid = self._order_id(order_record)
        record = self._ledger.get(oid)
        if not record:
            return
        if record.get("exit", {}).get("state") == ST_EXITED:
            logger.info(f"[sandbox_exec] EOD_CLOSE key={oid} already EXITED (sandbox 15:15) — no-op")
            return
        if record.get("entry", {}).get("state") == ST_ENTERED:
            logger.warning(
                f"[sandbox_exec] EOD_CLOSE key={oid} still ENTERED at dummy 15:35 "
                f"— forcing exit (missed 15:15 job?)"
            )
            self._handle_exit("EOD_CLOSE", order_record)

    def _handle_milestone(self, order_record: dict) -> None:
        oid = self._order_id(order_record)
        record = self._ledger.get(oid)
        if not record:
            return
        if record.get("entry", {}).get("state") != ST_ENTERED:
            return
        record["milestones"].append(
            {"at": _iso(_now()), "price": order_record.get("price")}
        )
        record["updated_at"] = _iso(_now())
        self._save()
        logger.info(
            f"[sandbox_exec] MILESTONE key={oid} (no broker action) price={order_record.get('price')}"
        )

    def force_eod_exit(self) -> dict:
        """Called by the scheduler at SANDBOX_EOD_HOUR:SANDBOX_EOD_MINUTE.
        Bypasses the normal-flow gate chain (safety: always try to flatten at EOD)."""
        with self._lock:
            self._load()
            open_recs = [
                r for r in self._ledger.values()
                if r.get("entry", {}).get("state") == ST_ENTERED
                and r.get("exit", {}).get("state") not in {ST_EXITED, ST_EXIT_PENDING}
            ]
            if not open_recs:
                logger.info("[sandbox_exec] force_eod_exit — no open positions")
                return {"status": "noop", "open": 0}
            for rec in open_recs:
                self._handle_exit("EOD_CLOSE", {
                    "order_id": rec["order_id"],
                    "instrument_key": rec["instrument_key"],
                    "bias": rec.get("bias"),
                    "entry": rec.get("dummy_entry_price"),
                })
            logger.info(f"[sandbox_exec] force_eod_exit processed {len(open_recs)} open position(s)")
            _tg_force_eod_exit(processed=len(open_recs))
            return {"status": "ok", "open": len(open_recs)}

    def reconcile_startup(self, dummy_summary: Optional[dict] = None) -> dict:
        """Run once per startup, after _dummy_order_catchup(). Policies:
        - PENDING_ENTRY  -> ENTRY_FAILED(restart_race); never retry entries.
        - EXIT_PENDING   -> retry exit (safe).
        - ENTERED today  -> if market open, exit now (reconciled_late_exit)."""
        with self._lock:
            self._load()
            summary = {
                "execution_enabled": SANDBOX_EXECUTION_ENABLED,
                "mode": SANDBOX_EXECUTION_MODE,
                "entered": 0,
                "exited_late": 0,
                "missed_entry": 0,
                "stuck": 0,
                "ledger_total": len(self._ledger),
            }
            if not SANDBOX_EXECUTION_ENABLED:
                logger.info("[sandbox_exec] startup reconcile — execution disabled")
                return summary

            today = _now().strftime("%Y-%m-%d")
            for rec in list(self._ledger.values()):
                if rec.get("trading_day") not in {today, None}:
                    continue
                es = rec.get("entry", {}).get("state")
                xs = rec.get("exit", {}).get("state")
                fake = {
                    "order_id": rec["order_id"],
                    "instrument_key": rec["instrument_key"],
                    "bias": rec.get("bias"),
                    "entry": rec.get("dummy_entry_price"),
                }
                if es == ST_PENDING_ENTRY:
                    rec["entry"]["state"] = ST_ENTRY_FAILED
                    rec["entry"]["error"] = "restart_race"
                    rec["updated_at"] = _iso(_now())
                    summary["missed_entry"] += 1
                elif xs == ST_EXIT_PENDING or (es == ST_ENTERED and xs not in {ST_EXITED, ST_EXIT_PENDING}):
                    self._handle_exit("RECONCILED_LATE_EXIT", fake)
                    summary["exited_late"] += 1
            if self._exit_stuck:
                summary["stuck"] = 1
            self._save()
            logger.info(f"[sandbox_exec] startup reconcile {summary}")
            _tg_reconcile_summary(summary=summary)
            return summary

    def resume(self, source: str = "api") -> dict:
        with self._lock:
            was_paused = self._paused
            self._paused = False
            self._pause_reason = None
            logger.info(f"[sandbox_exec] resumed from {source}")
            if was_paused:
                _tg(
                    "▶️ Sandbox · RESUMED",
                    f"<b>Source</b>: {_tg_escape(source)}",
                    level="INFO",
                )
            return self.status()

    def status(self) -> dict:
        with self._lock:
            self._load()
            today = _now().strftime("%Y-%m-%d")
            open_recs = [
                r for r in self._ledger.values()
                if r.get("entry", {}).get("state") == ST_ENTERED
                and r.get("exit", {}).get("state") not in {ST_EXITED, ST_EXIT_PENDING}
                and r.get("trading_day") in {today, None}
            ]
            todays = [r for r in self._ledger.values() if r.get("trading_day") in {today, None}]
            entered_today = [r for r in todays if r.get("entry", {}).get("state") == ST_ENTERED]
            exited_today = [r for r in todays if r.get("exit", {}).get("state") == ST_EXITED]
            entry_latencies = [
                int(r["entry"]["detection_to_fill_ms"])
                for r in entered_today
                if r.get("entry", {}).get("detection_to_fill_ms") is not None
            ]
            exit_latencies = [
                int(r["exit"]["detection_to_exit_ms"])
                for r in exited_today
                if r.get("exit", {}).get("detection_to_exit_ms") is not None
            ]

            def _stats(xs: list[int]) -> dict:
                if not xs:
                    return {"count": 0, "min": None, "max": None, "avg": None}
                return {"count": len(xs), "min": min(xs), "max": max(xs), "avg": sum(xs) / len(xs)}

            return {
                "enabled": SANDBOX_EXECUTION_ENABLED,
                "mode": SANDBOX_EXECUTION_MODE,
                "paused": self._paused,
                "pause_reason": self._pause_reason,
                "exit_stuck": self._exit_stuck,
                "open_position": open_recs[0]["order_id"] if open_recs else None,
                "ledger_total": len(self._ledger),
                "today": {"entries": len(entered_today), "exits": len(exited_today)},
                "entry_latency_ms": _stats(entry_latencies),
                "exit_latency_ms": _stats(exit_latencies),
                "last_error": self._last_error,
                "eod_schedule": f"{SANDBOX_EOD_HOUR:02d}:{SANDBOX_EOD_MINUTE:02d}",
                "ledger_path": str(SANDBOX_LEDGER_PATH),
            }


# ---------------------------------------------------------------------------
# Module-level singleton + registration helper
# ---------------------------------------------------------------------------

sandbox_execution_service = SandboxExecutionService()


def register_with_dummy_order_service() -> bool:
    """Called from main.py lifespan AFTER _dummy_order_catchup() completes.
    The dummy service must expose set_transition_callback(fn)."""
    try:
        from services.dummy_order_service import dummy_order_service  # local import (avoid cycle)
        setter = getattr(dummy_order_service, "set_transition_callback", None)
        if not callable(setter):
            logger.error(
                "[sandbox_exec] dummy_order_service has no set_transition_callback "
                "— live sandbox cannot receive transitions. Add it."
            )
            _tg(
                "❌ Sandbox · Registration failed",
                "<b>Reason</b>: <code>dummy_order_service</code> has no "
                "<code>set_transition_callback</code> — live sandbox cannot "
                "receive transitions.",
                level="ERROR",
            )
            return False
        setter(sandbox_execution_service.on_dummy_transition)
        logger.info("[sandbox_exec] transition callback registered on dummy_order_service")
        _tg(
            "🔗 Sandbox · Callback registered",
            (
                f"<b>Mode</b>: {_tg_escape(SANDBOX_EXECUTION_MODE)}\n"
                f"<b>Enabled</b>: {_tg_escape(SANDBOX_EXECUTION_ENABLED)}\n"
                f"<b>Lots</b>: {_tg_escape(SANDBOX_LOTS)}\n"
                f"<b>EOD</b>: {SANDBOX_EOD_HOUR:02d}:{SANDBOX_EOD_MINUTE:02d}"
            ),
            level="SUCCESS",
        )
        return True
    except Exception as e:
        logger.error(f"[sandbox_exec] registration failed: {e}", exc_info=True)
        _tg(
            "❌ Sandbox · Registration failed",
            f"<b>Error</b>:\n<pre>{_tg_escape(f'{type(e).__name__}: {e}')}</pre>",
            level="ERROR",
        )
        return False