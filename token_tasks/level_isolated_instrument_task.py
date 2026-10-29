# token_tasks/level_isolated_instrument_task.py
"""Scheduled wrapper + manual trigger for the level-isolation engine.

Used by:
  * scheduler_service — periodic recheck job (every 5 min during market)
  * routes/isolated_instrument_routes.py — manual backfill trigger

After every recheck (scheduled or manual), ``_try_promote()`` asks the
strategy-entry engine whether the current ARMED winner differs from the
official isolated instrument, and if so applies the promotion. This is
the 5-minute safety net so promotions land even when the browser is
closed.

Logging
-------
The recheck result line now includes the ``reason`` / ``skip_reason``
returned by the isolation service so operators can distinguish
``missing_inputs`` (self-heal territory) from ``outside_window`` (the
scheduled job fired before 09:16) from ``no_merged_data`` (the intraday
API returned no candles).

G1 v4.1 contract (2026-10-29)
-----------------------------
Three downstream fixes from the `/isolated` page analysis affect what
this task observes, without requiring any change to the orchestration
order used here:

* **§5.A — both-sides registration.** ``level_isolated_instrument_service._isolate()``
  now registers BOTH bias-side and counter-side instruments with
  ``live_sr_levels_service`` before returning. That means the
  ``strategy_entry_signal_service.compute()`` call below can resolve
  the counter-side candle close correctly on the *first* evaluation —
  previously the counter side fell back to live LTP because
  ``_candle_close_at()`` could not find the instrument's candle buffer.
  No change needed here: the compute call already runs *after*
  ``run_backfill()`` returns, and after §5.A the registration has
  already happened by then.

* **§5.E — frozen entry on the promotion record.** ``apply_promotion()``
  now writes ``record["frozen_entry"]`` with ``price`` /
  ``price_source`` / ``stop`` / ``target`` / ``time`` at the moment of
  promotion. The ``_try_promote()`` log line below now echoes those
  fields so an operator can verify (a) the frozen entry matches the
  dummy-order entry, and (b) the frozen entry was captured from
  ``candle_close_at_confirmed_fast`` (not ``live_ltp``). No change to
  the return-value contract — ``apply_promotion()`` still returns a
  dict whose ``promoted`` key the caller checks, and the dict now also
  carries ``frozen_entry`` when a promotion actually applied.

* **§5.B — historical entry price.** ``strategy_entry_signal_service._entry_price_for()``
  now prefers the candle close at ``confirmed_time_fast`` and only
  falls back to live LTP when that lookup genuinely fails. The
  ``frozen_entry.price_source`` field logged below is therefore
  expected to read ``candle_close_at_confirmed_fast`` after §5.B — a
  ``live_ltp`` value in the log means the fallback fired and warrants
  investigation.

If after deploying G1 v4.1 you see ``frozen_entry.price_source == "live_ltp"``
on the first promotion of a session, something upstream is still
broken. The dummy-order row may legitimately disagree with the frozen
entry for orders that were *created before* the deploy — that is a
legacy order, and the frozen entry is authoritative from this point
forward.

Self-heal
---------
``run_level_isolation_recheck`` delegates to
``level_isolated_instrument_service.run_backfill`` which (since the
2026-10-27 fix) calls ``_maybe_self_heal()`` internally when the
first_candle / level_instruments files are missing.

Since G1 v4.1 (§5.D) the self-heal also verifies the cached
``first_candle.json`` H/L against today's real first intraday bar
(``live_sr_calc_service.get_first_intraday_candle()``) and forces a
**targeted** recompute via
``first_candle_service.recompute_for_underlying()`` when they
disagree. This closes the "stale-but-today-dated OR file" hole that
produced the phantom MIDCPNIFTY R3 touch (trigger_ltp 13310.60 vs
R3 13329.65, with OR-H 13298.75) described in analysis §4.1 / §4.5.

This task file still does not need to do anything special for
self-heal — the service handles it before acquiring its lock.
"""

from __future__ import annotations

from core import config
from core.logger import get_logger
from services.isolated_instrument_promotion_service import (
    isolated_instrument_promotion_service,
)
from services.level_isolated_instrument_service import (
    level_isolated_instrument_service,
)
from services.strategy_entry_signal_service import (
    strategy_entry_signal_service,
)
from services.telegram_service import telegram_service

logger = get_logger(__file__)

_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"

def _try_promote() -> None:
    """Promote the entry-signal winner to be the active instrument.

    Idempotent per day when ``STRATEGY_ENTRY_ALLOW_RE_PROMOTION`` is False.
    Silently no-ops when:
      * strategy entry is disabled,
      * promotion is disabled,
      * no isolated instrument exists,
      * no side has ARMED yet,
      * a promotion has already been applied today (default).

    After G1 v4.1 (§5.E) the promotion service also freezes
    ``record["frozen_entry"]`` with the winning side's entry price /
    stop / target / source, and returns it on the result dict. The log
    line below echoes those fields so operators can verify the frozen
    entry against the dummy-order row without opening the /isolated
    page.
    """
    if not config.STRATEGY_ENTRY_ENABLED:
        return
    if not config.STRATEGY_ENTRY_ALLOW_PROMOTION:
        return
    try:
        signal = strategy_entry_signal_service.compute()
        result = isolated_instrument_promotion_service.apply_promotion(signal)
        if result.get("promoted"):
            promotion = result.get("promotion") or {}
            frozen = result.get("frozen_entry") or {}
            logger.info(
                "[level-isolation-task] promotion applied "
                "from=%s to=%s reason=%s "
                "frozen_entry={price=%s source=%s stop=%s target=%s time=%s}",
                promotion.get("from_instrument_key"),
                promotion.get("to_instrument_key"),
                promotion.get("reason"),
                frozen.get("price"),
                frozen.get("price_source"),
                frozen.get("stop"),
                frozen.get("target"),
                frozen.get("time"),
            )
    except Exception:
        logger.exception("[level-isolation-task] promotion step failed")

def run_level_isolation_recheck(reason: str = "scheduled") -> None:
    """Called by the scheduler every 5 minutes during market hours."""
    logger.info("[level-isolation-task] recheck start reason=%s", reason)
    try:
        result = level_isolated_instrument_service.run_backfill(reason=reason)
        status = result.get("status")
        # Surface the reason so ``missing_inputs`` / ``outside_window`` /
        # ``no_merged_data`` are distinguishable in the log stream.
        reason_val = (
            result.get("reason")
            or result.get("skip_reason")
            or "—"
        )
        logger.info(
            "[level-isolation-task] recheck result status=%s reason=%s",
            status, reason_val,
        )
    except Exception as exc:
        logger.exception("[level-isolation-task] recheck failed")
        try:
            telegram_service.send_message(
                "Level Isolation · Recheck Failed",
                f"{_DIV}\n❌ RECHECK FAILED\n{_DIV}\n"
                f"Error: {type(exc).__name__}: {exc}",
                level="ERROR",
            )
        except Exception:
            pass
        return

    # Promote the entry-signal winner (5-min safety net, in case the
    # browser is closed). After G1 v4.1 the compute() call resolves the
    # historical entry price via the newly-registered counter-side
    # candle buffer (see §5.A + §5.B), so the promotion this triggers
    # freezes the *correct* entry — not the 15:31 live LTP that the
    # /isolated page analysis caught (§3 / §4.4).
    _try_promote()

def run_level_isolation_backfill_manual() -> dict:
    """Manual backfill from the admin route. Never raises."""
    try:
        result = level_isolated_instrument_service.run_backfill(reason="manual")
        # Also run promotion on the manual path.
        _try_promote()
        return result
    except Exception as exc:
        logger.exception("[level-isolation-task] manual backfill failed")
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }