# services/feed_subscription_manager.py
"""Thread-safe live-feed subscription budget manager.

Implements the three-tier subscription policy from the 2026 capacity
analysis. Keeps the total number of subscribed instruments strictly
below ``LIVE_FEED_MAX_TOTAL_INSTRUMENTS`` (default 1800, safely under
the 2,000 hard cap on full mode so reconnects never briefly exceed it).

Tiers
-----
Tier 1 — Mandatory (never evicted)
    * Four index rows (NIFTY / BANKNIFTY / MIDCPNIFTY / SENSEX).
    * The health-feed instrument.
    * The currently isolated and/or promoted instrument.
    * Anything added via ``set_mandatory()`` or ``add_mandatory()``.
    * Every CE+PE strike present in each index's
      ``level_instruments.json`` (the R1/R2/R3/S1/S2/S3 contracts the
      isolation engine watches) — enabled by default via
      ``LIVE_FEED_LEVEL_INSTRUMENTS_MANDATORY``.

      These are registered separately via ``set_level_mandatory()`` /
      ``seed_level_mandatory_from_disk()`` and are immune to
      ``set_mandatory()``. Their purpose is to guarantee that the live
      SR engine keeps receiving ticks on exactly the CE/PE strikes the
      strategy is watching, even after the ATM band slides away from
      them mid-session. Without this, the strikes the isolation engine
      actually cares about become evictable (Tier 2) and can stop
      ticking before the strategy arms.

Tier 2 — Active window (evictable)
    * ATM ± N strike band per index, recomputed every
      ``LIVE_FEED_ATM_RECALC_SECONDS`` from the live index LTP.
    * Keys here are evictable (oldest first) when
      ``desired_keys()`` exceeds the cap.
    * Level_instruments strikes live in Tier 1 (see above), not here —
      so they cannot be evicted even if the ATM band slides past them.

Tier 3 — On-demand (LRU)
    * Instruments opened in a chart / layout / option-chain page.
    * Instruments the dummy-order service pins when it creates an order.
    * ``subscribe_instrument()`` calls from routes land here.

Overflow policy
---------------
When ``desired_keys()`` exceeds ``LIVE_FEED_MAX_TOTAL_INSTRUMENTS``:
    1. Evict the oldest on-demand key (LRU) — unless
       ``LIVE_FEED_EVICT_OLDEST_ON_OVERFLOW`` is false.
    2. If no on-demand keys remain, evict the oldest active-window
       key that is not also mandatory.
    3. If still over, log at WARNING and truncate. Mandatory keys
       (including level_instruments strikes) are never evicted.

Threading
---------
All public methods are thread-safe (guarded by ``self._lock``). The
``apply()`` method invokes a caller-supplied callback with the full
desired set; the caller is responsible for the diff and for calling
``streamer.subscribe()`` / ``streamer.unsubscribe()`` in batches.
The manager never touches the streamer directly.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# Canonical names -> Upstox instrument keys for the four monitored indices.
INDEX_CANONICAL_TO_KEY: dict[str, str] = {
    "NIFTY":      "NSE_INDEX|Nifty 50",
    "BANKNIFTY":  "NSE_INDEX|Nifty Bank",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "SENSEX":     "BSE_INDEX|SENSEX",
}

_KEY_TO_CANONICAL: dict[str, str] = {
    v: k for k, v in INDEX_CANONICAL_TO_KEY.items()
}


def _band_for(canonical: str) -> int:
    return {
        "NIFTY":      int(getattr(config, "LIVE_FEED_ATM_BAND_NIFTY", 10)),
        "BANKNIFTY":  int(getattr(config, "LIVE_FEED_ATM_BAND_BANKNIFTY", 5)),
        "MIDCPNIFTY": int(getattr(config, "LIVE_FEED_ATM_BAND_MIDCPNIFTY", 5)),
        "SENSEX":     int(getattr(config, "LIVE_FEED_ATM_BAND_SENSEX", 5)),
    }.get(canonical, 5)


def _strike_step_for(canonical: str) -> int:
    return {
        "NIFTY":      int(getattr(config, "LEVEL_FILTER_STRIKE_STEP_NIFTY", 50)),
        "BANKNIFTY":  int(getattr(config, "LEVEL_FILTER_STRIKE_STEP_BANKNIFTY", 100)),
        "MIDCPNIFTY": int(getattr(config, "LEVEL_FILTER_STRIKE_STEP_MIDCPNIFTY", 25)),
        "SENSEX":     int(getattr(config, "LEVEL_FILTER_STRIKE_STEP_SENSEX", 100)),
    }.get(canonical, 50)


def _today_market_iso() -> str:
    """Return today's date in the configured market timezone (IST)."""
    try:
        tz = ZoneInfo(getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata"))
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")
    return datetime.now(tz).date().isoformat()


def _level_instruments_path(canonical: str) -> Path:
    """Return ``<LIVE_DATA_DIR>/index/<canonical_lower>/level_instruments.json``."""
    return (
        Path(config.LIVE_DATA_DIR)
        / "index"
        / str(canonical).strip().lower()
        / "level_instruments.json"
    )


def _extract_level_keys_from_payload(payload: Any) -> set[str]:
    """Return the CE+PE instrument_key set from a level_instruments.json payload."""
    keys: set[str] = set()
    if not isinstance(payload, dict):
        return keys
    for bucket in ("ce_instruments", "pe_instruments"):
        items = payload.get(bucket) or []
        if not isinstance(items, list):
            continue
        for inst in items:
            if not isinstance(inst, dict):
                continue
            key = str(inst.get("instrument_key") or "").strip()
            if key:
                keys.add(key)
    return keys


class FeedSubscriptionManager:
    """Owns the three-tier subscription budget."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Base mandatory set (4 index rows + health feed + isolated + promoted
        # + anything added via add_mandatory). Replaced by set_mandatory().
        self._mandatory: set[str] = set()
        # Level-instruments mandatory set, keyed by canonical index name.
        # Populated by set_level_mandatory() / seed_level_mandatory_from_disk().
        # Immune to set_mandatory() so a refresh never wipes the level strikes.
        self._level_mandatory: dict[str, set[str]] = {}
        self._active_by_index: dict[str, set[str]] = {}
        self._on_demand: "OrderedDict[str, float]" = OrderedDict()
        self._last_atm_recalc: dict[str, float] = {}
        self._isolated_key: str | None = None
        self._promoted_key: str | None = None
        self._apply_callback: Callable[[set[str]], None] | None = None

    # ------------------------------------------------------------------ #
    # Tier 1 — mandatory                                                 #
    # ------------------------------------------------------------------ #
    def set_mandatory(self, keys: Iterable[str]) -> None:
        """Replace the **base** mandatory set outright.

        This does NOT touch the level-instruments mandatory set — those
        survive a base-set replacement. Use ``clear_level_mandatory()``
        to drop them explicitly.
        """
        new_set = {
            str(k).strip() for k in keys if str(k).strip()
        }
        with self._lock:
            self._mandatory = new_set
        logger.info(
            "[feed-subs] mandatory set — %d key(s)", len(new_set)
        )

    def add_mandatory(self, key: str) -> None:
        k = str(key or "").strip()
        if not k:
            return
        with self._lock:
            self._mandatory.add(k)

    def remove_mandatory(self, key: str) -> None:
        k = str(key or "").strip()
        with self._lock:
            self._mandatory.discard(k)

    def set_isolated(self, key: str | None) -> None:
        """Mark the isolated instrument — always in the desired set."""
        with self._lock:
            self._isolated_key = (
                str(key).strip() if key else None
            )

    def set_promoted(self, key: str | None) -> None:
        """Mark the promoted instrument — always in the desired set."""
        with self._lock:
            self._promoted_key = (
                str(key).strip() if key else None
            )

    # ------------------------------------------------------------------ #
    # Tier 1b — level-instruments mandatory                              #
    # ------------------------------------------------------------------ #
    def set_level_mandatory(
        self,
        canonical: str,
        keys: Iterable[str],
        *,
        trigger_apply: bool = True,
    ) -> dict[str, Any]:
        """Replace the level-instruments mandatory strike set for one index.

        These keys are added on top of the base mandatory set and are
        **never evicted** (same protection as the four index rows).
        They are intentionally separate from ``set_mandatory()`` so that
        replacing the base set never wipes the level strikes.

        Callers:
            * ``level_instrument_filter_service._register_with_sr()``
              (after the filter writes a fresh ``level_instruments.json``)
            * ``live_feed_service._build_initial_subscription_set()``
              (via ``seed_level_mandatory_from_disk()``)

        Parameters
        ----------
        canonical : str
            One of ``NIFTY`` / ``BANKNIFTY`` / ``MIDCPNIFTY`` / ``SENSEX``.
        keys : iterable of str
            Instrument keys to make mandatory. An empty iterable
            **removes** the level-mandatory set for this index.
        trigger_apply : bool
            When True (default) and the set actually changed, calls
            ``apply()`` so the new keys are pushed to the streamer.
            Pass False when batching multiple indices.

        Returns a dict summarising what changed.
        """
        canonical = str(canonical).strip().upper()
        new_set = {str(k).strip() for k in keys if str(k).strip()}

        with self._lock:
            previous = self._level_mandatory.get(canonical, set())
            if new_set:
                self._level_mandatory[canonical] = new_set
            else:
                self._level_mandatory.pop(canonical, None)
            added = new_set - previous
            removed = previous - new_set

        if added or removed:
            logger.info(
                "[feed-subs] level_mandatory %s — +%d -%d (size=%d)",
                canonical, len(added), len(removed), len(new_set),
            )

        if trigger_apply and (added or removed):
            self.apply()

        return {
            "canonical": canonical,
            "added": sorted(added),
            "removed": sorted(removed),
            "size": len(new_set),
        }

    def add_level_mandatory(
        self,
        canonical: str,
        keys: Iterable[str],
        *,
        trigger_apply: bool = True,
    ) -> dict[str, Any]:
        """Merge ``keys`` into the level-mandatory set for ``canonical``.

        Unlike ``set_level_mandatory()``, this does not remove existing
        keys — useful when an incremental filter run surfaces a new
        strike without invalidating the previous set.
        """
        canonical = str(canonical).strip().upper()
        added: set[str] = set()
        with self._lock:
            existing = self._level_mandatory.setdefault(canonical, set())
            for k in keys:
                key = str(k).strip()
                if key and key not in existing:
                    existing.add(key)
                    added.add(key)
            size = len(existing)

        if added:
            logger.info(
                "[feed-subs] level_mandatory %s — +%d (total=%d)",
                canonical, len(added), size,
            )

        if trigger_apply and added:
            self.apply()

        return {"canonical": canonical, "added": sorted(added), "size": size}

    def clear_level_mandatory(self, *, trigger_apply: bool = True) -> int:
        """Drop every level-instruments mandatory key. Returns count removed."""
        with self._lock:
            n = sum(len(s) for s in self._level_mandatory.values())
            self._level_mandatory.clear()
        if n:
            logger.info(
                "[feed-subs] level_mandatory cleared — %d key(s) removed", n,
            )
        if trigger_apply and n:
            self.apply()
        return n

    def seed_level_mandatory_from_disk(
        self,
        *,
        skip_stale: bool = True,
        trigger_apply: bool = False,
    ) -> dict[str, Any]:
        """Read every index's ``level_instruments.json`` and register its
        CE+PE strikes as mandatory.

        A missing file is skipped silently. A file whose ``date`` field
        does not equal today's IST date is skipped when ``skip_stale``
        is True (default) — this prevents a previous-day file that
        survived a restart from making stale strikes permanent.

        ``trigger_apply=False`` (default) lets the caller batch multiple
        seeds and apply once. Call ``apply()`` yourself afterwards (or
        pass ``trigger_apply=True``).

        Never raises. Returns a summary dict.
        """
        summary: dict[str, Any] = {
            "status": "ok",
            "per_index": {},
            "total": 0,
            "seeded_indices": 0,
            "errors": [],
            "skipped_stale": [],
        }

        if not bool(getattr(config, "LIVE_FEED_LEVEL_INSTRUMENTS_MANDATORY", True)):
            summary["status"] = "disabled"
            return summary

        today = _today_market_iso()

        for canonical in INDEX_CANONICAL_TO_KEY:
            try:
                path = _level_instruments_path(canonical)
                if not path.exists():
                    summary["per_index"][canonical] = 0
                    continue

                payload = json.loads(path.read_text(encoding="utf-8"))
                file_date = str(payload.get("date") or "")
                if skip_stale and file_date and file_date != today:
                    logger.info(
                        "[feed-subs] level_instruments.json for %s is stale "
                        "(date=%s, today=%s); not registering",
                        canonical, file_date, today,
                    )
                    summary["per_index"][canonical] = 0
                    summary["skipped_stale"].append(canonical)
                    continue

                keys = _extract_level_keys_from_payload(payload)
                self.set_level_mandatory(
                    canonical, keys, trigger_apply=False,
                )
                summary["per_index"][canonical] = len(keys)
                summary["total"] += len(keys)
                summary["seeded_indices"] += 1
            except Exception as exc:
                logger.exception(
                    "[feed-subs] seed level_instruments failed for %s",
                    canonical,
                )
                summary["errors"].append(
                    f"{canonical}: {type(exc).__name__}: {exc}"
                )
                summary["per_index"][canonical] = 0

        logger.info(
            "[feed-subs] level_instruments seed — total=%d across %d "
            "index(es), skipped_stale=%d errors=%d",
            summary["total"],
            summary["seeded_indices"],
            len(summary["skipped_stale"]),
            len(summary["errors"]),
        )

        if trigger_apply and summary["total"] > 0:
            self.apply()

        return summary

    def reload_level_mandatory_from_disk(self) -> dict[str, Any]:
        """Clear the level-mandatory set, then re-read every file.

        Convenience for hard-refresh / day-rollover paths: drop any
        existing level strikes then re-seed from disk and apply once.
        """
        removed = self.clear_level_mandatory(trigger_apply=False)
        result = self.seed_level_mandatory_from_disk(
            skip_stale=True, trigger_apply=False,
        )
        result["cleared"] = removed
        self.apply()
        return result

    # ------------------------------------------------------------------ #
    # Tier 3 — on-demand LRU                                             #
    # ------------------------------------------------------------------ #
    def pin(self, key: str) -> None:
        """Add / refresh ``key`` in the on-demand LRU."""
        k = str(key or "").strip()
        if not k:
            return
        with self._lock:
            self._on_demand[k] = time.monotonic()
            self._on_demand.move_to_end(k)
        logger.debug("[feed-subs] pinned %s", k)

    def unpin(self, key: str) -> None:
        k = str(key or "").strip()
        with self._lock:
            self._on_demand.pop(k, None)

    def is_pinned(self, key: str) -> bool:
        k = str(key or "").strip()
        with self._lock:
            return k in self._on_demand

    def _touch_pinned(self, key: str) -> None:
        with self._lock:
            if key in self._on_demand:
                self._on_demand[key] = time.monotonic()
                self._on_demand.move_to_end(key)

    # ------------------------------------------------------------------ #
    # Tier 2 — ATM band                                                  #
    # ------------------------------------------------------------------ #
    def recompute_atm_band(
        self,
        canonical: str,
        ltp: float,
        triples: list[tuple[float, str, str]],
    ) -> bool:
        """Recompute the ATM band for one index.

        ``triples`` is a list of ``(strike, type, instrument_key)``.
        Returns True if the active set for this index changed.
        """
        canonical = str(canonical).strip().upper()
        if canonical not in INDEX_CANONICAL_TO_KEY:
            return False
        if not getattr(config, "LIVE_FEED_ATM_BAND_ENABLED", True):
            return False
        band_n = _band_for(canonical)
        if band_n <= 0:
            return False
        try:
            ltp_f = float(ltp)
        except (TypeError, ValueError):
            return False
        if ltp_f <= 0:
            return False

        step = _strike_step_for(canonical)
        if step <= 0:
            step = 50
        atm = round(ltp_f / step) * step
        lo = atm - band_n * step
        hi = atm + band_n * step

        wanted: set[str] = set()
        for strike, _stype, key in triples:
            k = str(key or "").strip()
            if not k:
                continue
            try:
                s = float(strike)
            except (TypeError, ValueError):
                continue
            if lo <= s <= hi:
                wanted.add(k)

        with self._lock:
            previous = self._active_by_index.get(canonical, set())
            self._active_by_index[canonical] = wanted
            self._last_atm_recalc[canonical] = time.monotonic()

        changed = previous != wanted
        if changed:
            logger.info(
                "[feed-subs] ATM band %s — ltp=%.2f step=%d band=±%d "
                "strikes -> %d key(s) (was %d)",
                canonical, ltp_f, step, band_n, len(wanted), len(previous),
            )
        return changed

    def recompute_atm_band_from_registry(
        self,
        canonical: str,
        ltp: float,
        registry,
    ) -> bool:
        """Convenience wrapper: resolve triples from the instrument registry."""
        try:
            entries = registry.query(underlying=canonical) or []
        except Exception:
            logger.exception(
                "[feed-subs] registry query failed for %s", canonical
            )
            entries = []
        triples: list[tuple[float, str, str]] = []
        for e in entries:
            try:
                strike = float(e.get("strike_price"))
            except (TypeError, ValueError):
                continue
            stype = str(e.get("strike_type") or "").upper()
            key = str(e.get("instrument_key") or "").strip()
            if key:
                triples.append((strike, stype, key))
        return self.recompute_atm_band(canonical, ltp, triples)

    def recompute_atm_bands_from_registry(self, registry) -> None:
        """Seed all four indices from cached LTPs (used at start)."""
        if not getattr(config, "LIVE_FEED_ATM_BAND_ENABLED", True):
            return
        try:
            from services.live_feed_service import live_feed_service
        except Exception:
            logger.exception(
                "[feed-subs] live_feed_service import failed during seed"
            )
            return

        seeded = 0
        for canonical, index_key in INDEX_CANONICAL_TO_KEY.items():
            ltp = live_feed_service.get_latest_ltp(index_key)
            if ltp is None:
                continue
            if self.recompute_atm_band_from_registry(canonical, ltp, registry):
                seeded += 1
        logger.info(
            "[feed-subs] ATM band seed — %d index(es) populated", seeded
        )

    def on_index_tick(self, instrument_key: str, ltp: float) -> None:
        """Called by live_feed_service for every index tick.

        Throttled to ``LIVE_FEED_ATM_RECALC_SECONDS`` per index.
        """
        if not getattr(config, "LIVE_FEED_ATM_BAND_ENABLED", True):
            return
        canonical = _KEY_TO_CANONICAL.get(instrument_key)
        if canonical is None:
            return

        now = time.monotonic()
        interval = float(
            getattr(config, "LIVE_FEED_ATM_RECALC_SECONDS", 300)
        )
        with self._lock:
            last = self._last_atm_recalc.get(canonical, 0.0)
        if now - last < interval:
            return

        try:
            from services.instrument_registry import instrument_registry
        except Exception:
            logger.exception(
                "[feed-subs] instrument_registry import failed in tick"
            )
            return

        if self.recompute_atm_band_from_registry(
            canonical, ltp, instrument_registry
        ):
            # A band change means the desired set changed — reapply.
            self.apply()

    # ------------------------------------------------------------------ #
    # Desired-set computation                                            #
    # ------------------------------------------------------------------ #
    def _union_locked(self) -> tuple[set[str], set[str]]:
        mandatory = set(self._mandatory)

        # Level-instruments strikes are mandatory when the feature is on.
        # They live in a separate dict so set_mandatory() never wipes them.
        if bool(getattr(config, "LIVE_FEED_LEVEL_INSTRUMENTS_MANDATORY", True)):
            for s in self._level_mandatory.values():
                mandatory |= s

        if self._isolated_key:
            mandatory.add(self._isolated_key)
        if self._promoted_key:
            mandatory.add(self._promoted_key)

        active: set[str] = set()
        for s in self._active_by_index.values():
            active |= s

        on_demand = set(self._on_demand.keys())
        return mandatory, active | on_demand

    def desired_keys(self) -> list[str]:
        """Return the ordered, capped list of desired subscriptions."""
        with self._lock:
            mandatory, rest = self._union_locked()
            max_total = int(
                getattr(
                    config, "LIVE_FEED_MAX_TOTAL_INSTRUMENTS", 1800,
                )
            )
            if max_total <= 0:
                max_total = 1800

            desired = set(mandatory) | set(rest)

            if len(desired) <= max_total:
                return self._sorted(desired)

            evict = bool(
                getattr(
                    config,
                    "LIVE_FEED_EVICT_OLDEST_ON_OVERFLOW",
                    True,
                )
            )
            keep = set(desired)

            if not evict:
                logger.warning(
                    "[feed-subs] desired=%d exceeds max=%d but "
                    "eviction is disabled — truncating",
                    len(desired), max_total,
                )
                return self._sorted(list(keep)[:max_total])

            # 1) LRU eviction of on-demand keys (oldest first)
            for k in list(self._on_demand.keys()):
                if len(keep) <= max_total:
                    break
                keep.discard(k)

            # 2) Active-window eviction (oldest first, never mandatory)
            if len(keep) > max_total:
                active_ordered = sorted(
                    (
                        k for s in self._active_by_index.values()
                        for k in s
                    ),
                )
                for k in active_ordered:
                    if len(keep) <= max_total:
                        break
                    if k in mandatory:
                        continue
                    keep.discard(k)

            # 3) Last resort truncation
            if len(keep) > max_total:
                logger.warning(
                    "[feed-subs] still over cap — desired=%d max=%d "
                    "mandatory=%d; truncating",
                    len(keep), max_total, len(mandatory),
                )
                keep = set(list(keep)[:max_total])

            return self._sorted(keep)

    @staticmethod
    def _sorted(keys: Iterable[str]) -> list[str]:
        """Deterministic order: index rows first, then alphabetical."""
        index_priority = list(INDEX_CANONICAL_TO_KEY.values())

        def _rank(k: str) -> tuple[int, str]:
            try:
                return (index_priority.index(k), "")
            except ValueError:
                return (len(index_priority), k)

        return sorted((str(k) for k in keys if k), key=_rank)

    # ------------------------------------------------------------------ #
    # Apply                                                              #
    # ------------------------------------------------------------------ #
    def set_apply_callback(
        self, callback: Callable[[set[str]], None],
    ) -> None:
        """Register the callback that actually calls streamer.subscribe/unsubscribe.

        The callback receives the full desired set and must be
        responsible for computing the diff against what it currently
        has, batching SDK calls, and updating its own local tracking.
        It must never raise.
        """
        self._apply_callback = callback

    def apply(self) -> None:
        """Ask the registered callback to bring subscriptions in sync."""
        if self._apply_callback is None:
            return
        desired = set(self.desired_keys())
        try:
            self._apply_callback(desired)
        except Exception:
            logger.exception("[feed-subs] apply callback raised")

    # ------------------------------------------------------------------ #
    # Diagnostics                                                        #
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        with self._lock:
            mandatory, rest = self._union_locked()
            level_total = sum(len(s) for s in self._level_mandatory.values())
            return {
                "max_total": int(
                    getattr(
                        config, "LIVE_FEED_MAX_TOTAL_INSTRUMENTS", 1800,
                    )
                ),
                "atm_band_enabled": bool(
                    getattr(config, "LIVE_FEED_ATM_BAND_ENABLED", True)
                ),
                "mandatory_count": len(self._mandatory),
                "level_mandatory_enabled": bool(
                    getattr(config, "LIVE_FEED_LEVEL_INSTRUMENTS_MANDATORY", True)
                ),
                "level_mandatory_count": level_total,
                "level_mandatory_by_index": {
                    k: len(v) for k, v in self._level_mandatory.items()
                },
                "isolated_key": self._isolated_key,
                "promoted_key": self._promoted_key,
                "active_total": sum(
                    len(s) for s in self._active_by_index.values()
                ),
                "active_by_index": {
                    k: len(v) for k, v in self._active_by_index.items()
                },
                "on_demand_count": len(self._on_demand),
                "desired_total": len(mandatory | rest),
            }


feed_subscription_manager = FeedSubscriptionManager()