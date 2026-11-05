# services/order_runtime_service.py
"""Runtime order-enable toggle **and** post-11:30 trade tracker.

Two responsibilities live in this module:

1. **Runtime kill switch** (unchanged from the original file)

   ``order_runtime_service.is_enabled()`` is checked by every code
   path that would place, exit, or cancel an Upstox order. The flag
   is process-local and thread-safe. It resets to ENABLED on every
   restart — by design, so a crash / deploy does not leave the
   engine silently unable to trade.

   Public API::

       order_runtime_service.is_enabled() -> bool
       order_runtime_service.enable(source="api") -> dict
       order_runtime_service.disable(source="telegram") -> dict
       order_runtime_service.set(enabled, source="api") -> dict
       order_runtime_service.status() -> dict

2. **Post-11:30 trade tracking** (2026-11-05)

   When an order originating from the ``POST_1130_ISOLATED`` regime
   fills, its lifecycle is registered here so a later tick / candle
   update can be checked against the three exit conditions:

       * stop-loss hit       (``current_ltp <= sl_price``)
       * points target hit   (``current_ltp >= target_price``)
       * 15/15 resistance    (a confirmed 15/15 resistance whose
                              numeric price is at or below
                              ``current_ltp``)

   Trade state is stored per ``order_id`` under a dedicated lock,
   separate from the enable/disable toggle state. Two surfaces are
   provided:

     a. A **pure function** (module-level, spec-shaped)::

            evaluate_post_1130_exits(
                trade_state, current_candle, current_ltp,
                latest_15_15_sr,
            ) -> dict | None

        Callers who already hold a trade_state can call this directly
        and get back ``{"action": "EXIT", "reason": "<reason>", ...}``
        or ``None``.

     b. **Stateful helpers** on the service::

            order_runtime_service.register_post_1130_trade(...)
            order_runtime_service.get_post_1130_trade(order_id)
            order_runtime_service.list_post_1130_trades(status=None)
            order_runtime_service.mark_post_1130_closed(...)
            order_runtime_service.remove_post_1130_trade(order_id)
            order_runtime_service.clear_post_1130_trades()
            order_runtime_service.check_and_close_post_1130(...)

            # 2026-11-05 (persistence alignment)
            order_runtime_service.has_open_post_1130_trade(order_id=None)
            order_runtime_service.reconcile_from_order_records(records)

        ``check_and_close_post_1130`` is the standard tick-loop
        entry point: it looks up the state, calls the pure evaluator,
        and transitions ``status`` from ``OPEN`` → ``CLOSED`` when an
        exit fires (returning the exit reason + trade_state).

        ``reconcile_from_order_records`` is the **restart-recovery**
        bridge: it accepts an iterable of order records (typically
        ``dummy_orders.json["orders"]``) and ensures every OPEN
        POST-1130 record is registered here.  See the "Restart
        persistence" section below.

   All tracking state is process-local and in-memory only. It is
   deliberately NOT persisted in this module — the trade lifecycle
   mirrors a real broker order, which is itself only valid for the
   current session. Persistence lives in ``dummy_orders.json``; this
   module is the runtime projection of it.

   Restart persistence
   -------------------
   Because tracking is in-memory only, a mid-day restart would
   otherwise leave ``_post_1130_trades`` empty while the persisted
   ``dummy_orders.json`` still carries an OPEN POST-1130 order — so
   exits would silently never fire.

   Two complementary pieces close that gap:

     * ``dummy_order_service._ensure_post_1130_trade_registered``
       re-registers the trade on every tick (idempotent per
       ``order_id``), so the runtime map is repaired within one
       30 s scheduler tick after restart.

     * ``reconcile_from_order_records`` is the bulk version — a
       single call that brings the runtime map in line with a list
       of order records.  Useful for:
         - the ``dummy_order_service`` startup path,
         - the sandbox executor after a restart,
         - admin / ops tooling that wants to force-reconcile.

   ``register_post_1130_trade`` is idempotent: calling it twice for
   the same ``order_id`` is a no-op that returns the existing record.
   That makes every reconciliation path safe to run on every tick.

   Thread-safety
   -------------
   Both the toggle state and the trade map live behind a shared
   ``RLock``, so an evaluate-then-close sequence is atomic with
   respect to a concurrent register / clear from another thread.

Status payload (``status()``) — extended, backward-compatible::

    {
      "enabled": bool,
      "changed_at": ISO | None,
      "changed_by": str | None,
      "default_on_startup": True,
      # New in 2026-11-05 — additive, does not break old consumers.
      "post_1130_trades": {
          "open": int,
          "closed": int,
          "total": int,
      },
    }
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Small helpers                                                              #
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO string / numeric epoch / datetime into a tz-aware dt."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    try:
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        if n > 1e9:
            return datetime.fromtimestamp(n, tz=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _extract_level_price(sr: dict[str, Any]) -> float | None:
    """Return the numeric price for an SR level dict.

    Handles both the enriched shape (``level_price`` / ``price``) and
    the legacy shape where ``level`` is a numeric string. Returns
    ``None`` if no usable numeric price can be extracted — importantly,
    the string ``"resistance"`` will not be coerced to ``None`` /
    ``0.0``; it is simply rejected.
    """
    if not isinstance(sr, dict):
        return None
    for key in ("level_price", "price"):
        v = _to_float(sr.get(key))
        if v is not None:
            return v
    # Legacy fallback — the spec's example uses ``sr["level"]`` as the
    # numeric price.  Only accept it when it actually parses as a
    # float, so ``level="resistance"`` never leaks through.
    v = _to_float(sr.get("level"))
    return v

def _extract_level_type(sr: dict[str, Any]) -> str | None:
    """Return the SR type as a lowercase string ("support"/"resistance").

    Handles both the enriched shape (``type``) and the legacy shape
    where ``level`` holds the type string.  Only string ``level``
    values are treated as a type — numeric ``level`` values are
    prices and must not be misread as types.
    """
    if not isinstance(sr, dict):
        return None
    t: Any = sr.get("type")
    if t is None:
        legacy = sr.get("level")
        if isinstance(legacy, str):
            t = legacy
    if t is None:
        return None
    s = str(t).strip().lower()
    return s if s in ("support", "resistance") else None

def _extract_confirmation_time(sr: dict[str, Any]) -> datetime | None:
    """Return the confirmation datetime for an SR level dict.

    Accepts the spec's ``confirmation_time`` field and the legacy
    ``confirmed_time`` alias.  Returns ``None`` if neither is
    present / parseable.
    """
    if not isinstance(sr, dict):
        return None
    for key in ("confirmation_time", "confirmed_time"):
        dt = _parse_dt(sr.get(key))
        if dt is not None:
            return dt
    return None

def _extract_effective_ltp(
    current_ltp: Any,
    current_candle: Any,
) -> float | None:
    """Return the LTP to use for exit evaluation.

    Preference: ``current_ltp`` when it parses as a positive float;
    otherwise the ``close`` field of ``current_candle`` when that is
    a dict.  Returns ``None`` when neither source yields a usable
    number — the caller should treat ``None`` as "cannot evaluate
    right now" and retry on the next tick.
    """
    ltp = _to_float(current_ltp)
    if ltp is not None and ltp > 0:
        return ltp
    if isinstance(current_candle, dict):
        c = _to_float(current_candle.get("close"))
        if c is not None and c > 0:
            return c
    return None

# --------------------------------------------------------------------------- #
# Pure exit evaluator (spec shape)                                           #
# --------------------------------------------------------------------------- #
def evaluate_post_1130_exits(
    trade_state: dict[str, Any],
    current_candle: Any,
    current_ltp: Any,
    latest_15_15_sr: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Evaluate the three post-11:30 exit conditions against one tick.

    Parameters
    ----------
    trade_state : dict
        The dict produced by ``register_post_1130_trade``.  Requires
        ``entry_price``, ``sl_price``, ``target_price``.  Optional
        ``entry_time`` (used to skip pre-entry resistances when the
        caller has supplied it — see Note below).
    current_candle : dict | None
        The just-closed (or in-progress) candle.  Used as an LTP
        fallback when ``current_ltp`` is missing.
    current_ltp : float | str | None
        The live last-traded-price.  Preferred over
        ``current_candle["close"]``.
    latest_15_15_sr : list[dict] | None
        The most recent set of ``(15, 15)``-confirmed SR levels for
        the traded instrument.  May be the enriched shape
        (``level_price`` / ``type``) or the legacy shape
        (``price`` / ``level``).

    Returns
    -------
    ``{"action": "EXIT", "reason": <reason>, ...}`` when any condition
    fires, otherwise ``None``.

    Reasons (stable strings — safe for downstream routing):
        * ``"STOPLOSS_30_PCT_HIT"``      — ``current_ltp <= sl_price``
        * ``"TARGET_50_POINTS_HIT"``     — ``current_ltp >= target_price``
                                           (or ``current_ltp - entry``
                                           ``>= 50`` when target_price
                                           is missing)
        * ``"RESISTANCE_15_15_CONFIRMED"`` — a confirmed 15/15
                                             resistance priced at or
                                             below ``current_ltp``

    Note
    ----
    When ``trade_state`` carries ``entry_time`` **and** the SR level
    carries a parseable ``confirmation_time``, resistances that
    confirmed STRICTLY BEFORE ``entry_time`` are ignored.  This
    matches the strategy's post-entry-only semantics.  When either
    timestamp is missing the check is skipped and the function
    behaves exactly as the spec's sample.
    """
    if not isinstance(trade_state, dict):
        return None

    entry_price = _to_float(trade_state.get("entry_price"))
    sl_price = _to_float(trade_state.get("sl_price"))
    target_price = _to_float(trade_state.get("target_price"))
    if entry_price is None or entry_price <= 0:
        return None

    ltp = _extract_effective_ltp(current_ltp, current_candle)
    if ltp is None:
        return None

    # ---- 1. Stoploss (fixed 30 % below fill by default) ------------- #
    if sl_price is not None and ltp <= sl_price:
        return {
            "action": "EXIT",
            "reason": "STOPLOSS_30_PCT_HIT",
            "exit_price": ltp,
            "sl_price": sl_price,
        }

    # ---- 2. Points target ------------------------------------------- #
    if target_price is not None:
        target_hit = ltp >= target_price
    else:
        # Fallback path (target_price missing from state).
        target_points = float(
            getattr(config, "POST_1130_TARGET_POINTS", 50.0)
        )
        target_hit = (ltp - entry_price) >= target_points
    if target_hit:
        return {
            "action": "EXIT",
            "reason": "TARGET_50_POINTS_HIT",
            "exit_price": ltp,
            "target_price": target_price,
        }

    # ---- 3. 15/15 resistance confirmation --------------------------- #
    entry_dt = _parse_dt(trade_state.get("entry_time"))
    for sr in latest_15_15_sr or []:
        if _extract_level_type(sr) != "resistance":
            continue
        if str(sr.get("status") or "").strip().lower() != "confirmed":
            continue

        # Post-entry-only refinement — only applied when BOTH
        # ``entry_time`` (in trade_state) and ``confirmation_time``
        # (on the SR level) are parseable.  Otherwise the spec's
        # simpler filter stands unchanged.
        if entry_dt is not None:
            sr_conf_dt = _extract_confirmation_time(sr)
            if sr_conf_dt is not None and sr_conf_dt <= entry_dt:
                continue

        level_px = _extract_level_price(sr)
        if level_px is None:
            continue
        if ltp >= level_px:
            return {
                "action": "EXIT",
                "reason": "RESISTANCE_15_15_CONFIRMED",
                "exit_price": ltp,
                "resistance_price": level_px,
                "confirmation_time": (
                    sr.get("confirmation_time")
                    or sr.get("confirmed_time")
                ),
            }

    return None

# --------------------------------------------------------------------------- #
# Service                                                                    #
# --------------------------------------------------------------------------- #
class OrderRuntimeService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._enabled: bool = True                 # default ON at startup
        self._changed_at: str | None = None
        self._changed_by: str | None = None
        # Post-11:30 trade map: order_id -> trade_state dict.
        self._post_1130_trades: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Runtime enable / disable toggle (unchanged API)                    #
    # ------------------------------------------------------------------ #
    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def enable(self, source: str = "api") -> dict[str, Any]:
        return self.set(True, source=source)

    def disable(self, source: str = "api") -> dict[str, Any]:
        return self.set(False, source=source)

    def set(self, enabled: bool, source: str = "api") -> dict[str, Any]:
        with self._lock:
            previous = self._enabled
            self._enabled = bool(enabled)
            if previous != self._enabled:
                self._changed_at = datetime.now(timezone.utc).isoformat()
                self._changed_by = str(source or "api")
                logger.info(
                    "[order-runtime] enabled=%s changed_by=%s (was %s)",
                    self._enabled, self._changed_by, previous,
                )
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            open_n = sum(
                1 for t in self._post_1130_trades.values()
                if str(t.get("status") or "").upper() == "OPEN"
            )
            closed_n = sum(
                1 for t in self._post_1130_trades.values()
                if str(t.get("status") or "").upper() == "CLOSED"
            )
            return {
                "enabled": self._enabled,
                "changed_at": self._changed_at,
                "changed_by": self._changed_by,
                "default_on_startup": True,
                "post_1130_trades": {
                    "open": open_n,
                    "closed": closed_n,
                    "total": open_n + closed_n,
                },
            }

    # ------------------------------------------------------------------ #
    # Post-11:30 trade registration                                      #
    # ------------------------------------------------------------------ #
    def register_post_1130_trade(
        self,
        order_id: str,
        fill_price: float,
        *,
        entry_time: Any = None,
        trading_symbol: str | None = None,
        instrument_key: str | None = None,
        side: str | None = None,
        regime: str = "POST_1130_ISOLATED",
    ) -> dict[str, Any]:
        """Register a freshly-filled post-11:30 trade.

        Builds the trade_state dict per the spec:

            ``sl_price``   = ``round(fill_price * (1 - SL_PCT), 2)``
            ``target_price`` = ``round(fill_price + TARGET_POINTS, 2)``
            ``exit_sr_config`` = ``POST_1130_EXIT_SR_CONFIG``
                                 (fallback ``(15, 15)``)

        Idempotent per ``order_id``: if the same ``order_id`` is
        already registered, the existing state is returned unchanged
        and a WARNING is logged.  Callers who want to reset a trade
        should call ``remove_post_1130_trade`` first.

        Idempotency is what makes the restart-recovery flow (see
        ``reconcile_from_order_records`` and
        ``dummy_order_service._ensure_post_1130_trade_registered``)
        safe to run on every tick.

        Raises
        ------
        ``ValueError`` when ``fill_price`` is not a positive number.
        """
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError(
                "register_post_1130_trade: order_id must be a non-empty string"
            )
        fill = _to_float(fill_price)
        if fill is None or fill <= 0:
            raise ValueError(
                f"register_post_1130_trade: invalid fill_price={fill_price!r}"
            )

        sl_pct = float(getattr(config, "POST_1130_STOPLOSS_PERCENT", 0.30))
        target_points = float(
            getattr(config, "POST_1130_TARGET_POINTS", 50.0)
        )

        exit_cfg_raw = getattr(
            config, "POST_1130_EXIT_SR_CONFIG", (15, 15)
        )
        try:
            exit_cfg = (int(exit_cfg_raw[0]), int(exit_cfg_raw[1]))
        except (TypeError, ValueError, IndexError):
            exit_cfg = (15, 15)

        with self._lock:
            existing = self._post_1130_trades.get(order_key)
            if existing is not None:
                existing_fill = _to_float(existing.get("entry_price"))
                if (
                    existing_fill is not None
                    and abs(existing_fill - fill) > 1e-9
                ):
                    logger.warning(
                        "[order-runtime] register_post_1130_trade: "
                        "order_id=%s already registered with "
                        "entry_price=%.4f (new fill=%.4f) — keeping "
                        "the original record",
                        order_key, existing_fill, fill,
                    )
                else:
                    logger.info(
                        "[order-runtime] register_post_1130_trade: "
                        "order_id=%s already registered — idempotent "
                        "no-op",
                        order_key,
                    )
                return dict(existing)

            entry_dt = _parse_dt(entry_time)
            entry_time_iso = (
                entry_dt.astimezone(timezone.utc).isoformat()
                if entry_dt is not None else None
            )

            state: dict[str, Any] = {
                "order_id": order_key,
                "regime": str(regime or "POST_1130_ISOLATED"),
                "entry_price": float(fill),
                "sl_price": round(fill * (1.0 - sl_pct), 2),
                "target_price": round(fill + target_points, 2),
                "exit_sr_config": exit_cfg,
                "status": "OPEN",
                # Optional context fields (additive — do not affect
                # the spec's required keys).
                "entry_time": entry_time_iso,
                "trading_symbol": trading_symbol,
                "instrument_key": instrument_key,
                "side": str(side).upper() if side else None,
                "sl_pct": sl_pct,
                "target_points": target_points,
                "created_at": _now_iso(),
                "closed_at": None,
                "exit_reason": None,
                "exit_price": None,
            }
            self._post_1130_trades[order_key] = state

        logger.info(
            "[order-runtime] POST-1130 trade registered — "
            "order_id=%s fill=%.4f sl=%.4f target=%.4f exit_sr=%s",
            order_key, fill, state["sl_price"],
            state["target_price"], exit_cfg,
        )
        return dict(state)

    # ------------------------------------------------------------------ #
    # Post-11:30 trade accessors                                         #
    # ------------------------------------------------------------------ #
    def get_post_1130_trade(self, order_id: str) -> dict[str, Any] | None:
        """Return a copy of the trade_state, or ``None`` if unknown."""
        with self._lock:
            state = self._post_1130_trades.get(str(order_id or "").strip())
            return dict(state) if state is not None else None

    def list_post_1130_trades(
        self,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return copies of all trade_states.

        ``status`` filters on a case-insensitive match of
        ``trade_state["status"]`` (``"OPEN"`` / ``"CLOSED"``).
        """
        want = str(status).strip().upper() if status else None
        with self._lock:
            out: list[dict[str, Any]] = []
            for state in self._post_1130_trades.values():
                if want is not None:
                    have = str(state.get("status") or "").upper()
                    if have != want:
                        continue
                out.append(dict(state))
            return out

    def has_open_post_1130_trade(
        self,
        order_id: str | None = None,
    ) -> bool:
        """Return True iff there is at least one OPEN post-1130 trade.

        * ``order_id=None`` — True iff ANY trade in the map is OPEN.
          Useful as a single-call "does this session have live
          POST-1130 exposure" check for admin / ops dashboards.

        * ``order_id="<id>"`` — True iff THAT specific trade exists
          and is OPEN.  Convenience over ``get_post_1130_trade`` +
          status comparison, and useful for the tick-loop's
          "should I call ``check_and_close_post_1130``?" guard.

        Added 2026-11-05 as part of the restart-persistence alignment
        (see module docstring).
        """
        with self._lock:
            if order_id is None:
                return any(
                    str(t.get("status") or "").upper() == "OPEN"
                    for t in self._post_1130_trades.values()
                )
            key = str(order_id).strip()
            state = self._post_1130_trades.get(key)
            if state is None:
                return False
            return str(state.get("status") or "").upper() == "OPEN"

    def mark_post_1130_closed(
        self,
        order_id: str,
        exit_reason: str,
        exit_price: Any = None,
    ) -> dict[str, Any] | None:
        """Transition an OPEN trade to CLOSED with the given reason.

        Returns the updated trade_state, or ``None`` when the
        ``order_id`` is unknown.  Idempotent: if the trade is already
        CLOSED, the existing state is returned unchanged (the
        original close reason and price are retained).
        """
        key = str(order_id or "").strip()
        with self._lock:
            state = self._post_1130_trades.get(key)
            if state is None:
                return None
            if str(state.get("status") or "").upper() == "CLOSED":
                return dict(state)
            state["status"] = "CLOSED"
            state["exit_reason"] = str(exit_reason or "UNKNOWN")
            px = _to_float(exit_price)
            if px is not None:
                state["exit_price"] = float(px)
            state["closed_at"] = _now_iso()
            updated = dict(state)

        logger.info(
            "[order-runtime] POST-1130 trade CLOSED — "
            "order_id=%s reason=%s exit_price=%s",
            key, updated.get("exit_reason"), updated.get("exit_price"),
        )
        return updated

    def remove_post_1130_trade(
        self,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Delete a trade_state record entirely.

        Returns the removed state, or ``None`` when unknown.
        """
        key = str(order_id or "").strip()
        with self._lock:
            return self._post_1130_trades.pop(key, None)

    def clear_post_1130_trades(self) -> dict[str, Any]:
        """Drop every tracked trade_state (test / day-rollover helper)."""
        with self._lock:
            n = len(self._post_1130_trades)
            self._post_1130_trades.clear()
        logger.info(
            "[order-runtime] cleared %d post-1130 trade(s)", n,
        )
        return {"status": "cleared", "removed": n}

    # ------------------------------------------------------------------ #
    # Restart persistence alignment (2026-11-05)                         #
    # ------------------------------------------------------------------ #
    def reconcile_from_order_records(
        self,
        order_records: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Bring the in-memory trade map in line with a list of order records.

        This is the **bulk** counterpart to
        ``dummy_order_service._ensure_post_1130_trade_registered``
        (which reconciles one record at a time).  Both live behind the
        same idempotent ``register_post_1130_trade`` call, so running
        either (or both) is safe on every tick.

        Use cases:

            * ``dummy_order_service`` startup — reconcile once
              after loading ``dummy_orders.json`` so that any OPEN
              POST-1130 order is immediately tracked for exits
              before the first tick.

            * Sandbox executor startup — same idea for the mirror
              ledger.

            * Admin / ops tooling — force a reconcile after a manual
              state edit.

        Filtering
        ---------
        A record is **considered** when it satisfies:

            * ``order_id`` is a non-empty string,
            * ``status == "OPEN"`` (case-insensitive),
            * ``regime == "POST_1130_ISOLATED"`` OR ``regime`` is
              absent (backward compat — pre-2026-11-05 records
              predate the field),
            * ``entry_price`` parses as a positive float.

        For each qualifying record:

            * already registered → **no-op** (idempotent),
            * not registered → ``register_post_1130_trade(...)`` is
              called with the record's ``entry_price``,
              ``entry_time``, ``trading_symbol``,
              ``instrument_key``, and (best-effort) ``side``.

        Returns
        -------
        ``dict`` summary::

            {
              "considered":         int,  # records that passed the filter
              "already_registered": int,  # of those, already in the map
              "registered":         int,  # of those, newly added
              "skipped_invalid":    int,  # records that failed the filter
            }

        Never raises.  Per-record failures are logged and counted as
        ``skipped_invalid``.
        """
        summary = {
            "considered": 0,
            "already_registered": 0,
            "registered": 0,
            "skipped_invalid": 0,
        }

        if not order_records:
            return summary
        if not isinstance(order_records, (list, tuple)):
            logger.warning(
                "[order-runtime] reconcile_from_order_records: "
                "expected list/tuple, got %s — skipping",
                type(order_records).__name__,
            )
            return summary

        for raw in order_records:
            if not isinstance(raw, dict):
                summary["skipped_invalid"] += 1
                continue

            order_id = str(raw.get("order_id") or "").strip()
            if not order_id:
                summary["skipped_invalid"] += 1
                continue

            status = str(raw.get("status") or "").strip().upper()
            if status != "OPEN":
                summary["skipped_invalid"] += 1
                continue

            regime = str(raw.get("regime") or "").strip().upper()
            # Backward compat: pre-2026-11-05 records carry no
            # ``regime`` field — accept them but only if the caller
            # explicitly passed a POST-1130 list (the caller owns
            # that decision).  When ``regime`` IS present, filter on
            # it to avoid accidentally registering Path A/B orders.
            if regime and regime != "POST_1130_ISOLATED":
                summary["skipped_invalid"] += 1
                continue

            fill = _to_float(raw.get("entry_price"))
            if fill is None or fill <= 0:
                summary["skipped_invalid"] += 1
                continue

            summary["considered"] += 1

            # Idempotent check — cheap; the register call below is
            # also idempotent, but we need to distinguish "already
            # there" from "just added" for the summary.
            already = self.get_post_1130_trade(order_id) is not None
            if already:
                summary["already_registered"] += 1
                continue

            try:
                self.register_post_1130_trade(
                    order_id=order_id,
                    fill_price=fill,
                    entry_time=raw.get("entry_time"),
                    trading_symbol=raw.get("trading_symbol"),
                    instrument_key=raw.get("instrument_key"),
                    side=raw.get("side"),
                    regime=regime or "POST_1130_ISOLATED",
                )
                summary["registered"] += 1
            except Exception:
                logger.exception(
                    "[order-runtime] reconcile_from_order_records: "
                    "register_post_1130_trade failed for %s",
                    order_id,
                )
                summary["skipped_invalid"] += 1

        if summary["registered"] or summary["already_registered"]:
            logger.info(
                "[order-runtime] reconcile_from_order_records — "
                "considered=%d registered=%d already_registered=%d "
                "skipped_invalid=%d",
                summary["considered"],
                summary["registered"],
                summary["already_registered"],
                summary["skipped_invalid"],
            )
        return summary

    # ------------------------------------------------------------------ #
    # Post-11:30 tick-loop convenience                                   #
    # ------------------------------------------------------------------ #
    def check_and_close_post_1130(
        self,
        order_id: str,
        current_candle: Any,
        current_ltp: Any,
        latest_15_15_sr: list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """Evaluate one tick against a tracked trade and close it on exit.

        On an exit trigger:
            1. ``mark_post_1130_closed`` transitions the state.
            2. The result dict is augmented with the closed
               ``trade_state`` so callers can act on it directly.

        Returns
        -------
        ``None``       — no exit fired this tick, or ``order_id`` is
                         unknown.
        ``dict``       — the exit result: ``{"action": "EXIT",
                         "reason": ..., "trade_state": {...}, ...}``

        Safe to call even when the trade isn't yet registered — the
        method returns ``None`` and the caller's reconciliation path
        (see ``reconcile_from_order_records``) is expected to register
        it on the next tick.
        """
        key = str(order_id or "").strip()
        if not key:
            return None

        with self._lock:
            state = self._post_1130_trades.get(key)
            if state is None:
                return None
            if str(state.get("status") or "").upper() != "OPEN":
                return None
            # Snapshot for the evaluator (never let the pure function
            # mutate our stored state).
            snapshot = dict(state)

        result = evaluate_post_1130_exits(
            snapshot, current_candle, current_ltp, latest_15_15_sr,
        )
        if result is None:
            return None

        closed_state = self.mark_post_1130_closed(
            key,
            exit_reason=str(result.get("reason") or "UNKNOWN"),
            exit_price=result.get("exit_price"),
        )

        out = dict(result)
        out["trade_state"] = closed_state or snapshot
        return out

order_runtime_service = OrderRuntimeService()