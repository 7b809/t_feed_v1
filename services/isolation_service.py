# services/isolation_service.py
"""Isolation orchestrator.

Flow (v2):
  1. Skip if today's isolation already exists.
  2. Detect touches from OR levels (services/index_touch_detector).
  3. Arbitrate (services/touch_arbitration_service).
  4. Determine NIFTY trend (core/nifty_trend_service) → bias = CE/PE.
  5. Compute strike = round(NIFTY_LTP / NIFTY_STRIKE_STEP) * step.
  6. Search option at that strike (core/instrument_search_service).
  7. Snapshot option LTP from feed.
  8. Pin execution mode (services/execution_mode_service).
  9. Persist record with a detailed `explanation` sub-document.

Touch lifecycle
---------------
A touch that produces a successful isolation is added to
``_processed_touches`` and never re-attempted for the rest of the day
(moot anyway once ``_already_isolated_today()`` returns True, but it
keeps the state machine explicit).

A touch that **fails** to produce an isolation (search returns nothing,
NIFTY LTP unavailable, transient provider error, …) is **not** marked as
processed. Instead its key is placed in ``_retry_cooldown`` with a
``_FAILED_TOUCH_RETRY_SECONDS`` back-off so the next scan tick does not
hammer the same search endpoint. After the cooldown expires the touch
is retried on the normal 30 s scan cadence. This keeps a transient
failure from permanently suppressing an otherwise-valid isolation.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from core.config import (
    TIMEZONE,
    PIVOT_DOC_ID,
    ISOLATION_DOC_ID,
    PROJECT_INFO_COLLECTION,
    STRATEGY_RELATIONAL_R3_BIAS,
    STRATEGY_RELATIONAL_S3_BIAS,
    ISOLATION_BIAS_SOURCE,
    ISOLATION_OPTION_LTP_WAIT_SECONDS,
    NIFTY_STRIKE_STEP,
    INSTRUMENT_KEYS,
)
from core.instrument_search_service import (
    InstrumentSearchError,
    InstrumentSearchService,
    compute_atm_strike,
)
from core.logger import get_logger
from core.market_feed_service import MarketFeedService
from core.mongo_client import get_mongo_db
from core.nifty_trend_service import NiftyTrendService

from repositories.isolation_repository import IsolationRepository
from services.execution_mode_service import pin_execution_mode
from services.index_touch_detector import IndexTouchDetector
from services.touch_arbitration_service import arbitrate_touches
from services.isolation_notification_service import notify_isolation_clients

logger = get_logger(__file__)

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"

# Back-off applied to a touch whose isolation attempt failed (search
# returned nothing, NIFTY LTP unavailable, transient provider error, …).
# Keeps the 30 s scan tick from hammering the search API while a
# temporary failure exists, without permanently suppressing the touch.
# 300 s = 5 minutes.
_FAILED_TOUCH_RETRY_SECONDS = 300


class IsolationService:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.detector = IndexTouchDetector()
        self.search = InstrumentSearchService(access_token)
        self.trend = NiftyTrendService(access_token)
        self.repo = IsolationRepository()

        # Touches that have produced a successful isolation. Never
        # re-attempted.
        self._processed_touches: set[str] = set()

        # Touches whose isolation attempt failed, mapped to the epoch
        # second at which they become eligible for retry. A touch is
        # blocked from re-attempt until ``time.time() >= retry_after``.
        self._retry_cooldown: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    def scan_once(self, feed: MarketFeedService) -> Optional[dict]:
        """One scan tick.

        Returns the newly-created isolation record on success, ``None``
        otherwise. On failure, the touches that were attempted are placed
        in a retry cooldown so subsequent ticks do not immediately retry
        them — but they are **not** permanently suppressed.
        """
        if self._already_isolated_today():
            return None
        if not feed or not feed.get_health().get("running"):
            return None

        pivot_levels = self._load_pivot_levels()
        if not pivot_levels:
            logger.warning("OR levels unavailable; skipping scan")
            return None

        health = feed.get_health()
        touches = self._detect_touches(pivot_levels, health)
        if not touches:
            return None

        now_ts = time.time()
        new_touches: list[dict] = []
        for t in touches:
            key = f"{t['canonical']}_{t['side']}"
            if key in self._processed_touches:
                continue
            retry_after = self._retry_cooldown.get(key, 0.0)
            if now_ts < retry_after:
                continue
            new_touches.append(t)

        if not new_touches:
            return None

        # Log only the touches we are actually about to act on. Logging
        # every detected touch on every tick (as the old code did inside
        # `_detect_touches`) flooded the log with dozens of duplicate
        # lines per touch per minute.
        for t in new_touches:
            logger.info(
                "Touch detected: %s/%s at %s (LTP=%.2f vs level=%.2f)",
                t["canonical"], t["side"], t["trigger_level_name"],
                t["index_ltp_at_touch"], t["trigger_level"],
            )

        winner = arbitrate_touches(new_touches)
        if winner is None:
            # Defensive: arbitrate_touches returns None only on empty
            # input, which we already excluded above.
            return None

        record = self._create_isolation(winner, feed, backfilled=False)

        if record is not None:
            # Success — mark the touch set as permanently consumed.
            for t in new_touches:
                key = f"{t['canonical']}_{t['side']}"
                self._processed_touches.add(key)
                self._retry_cooldown.pop(key, None)
        else:
            # Failure — schedule a retry, do NOT mark as processed.
            retry_at = now_ts + _FAILED_TOUCH_RETRY_SECONDS
            for t in new_touches:
                key = f"{t['canonical']}_{t['side']}"
                self._retry_cooldown[key] = retry_at
            logger.warning(
                "Isolation creation FAILED for winner %s/%s; touches NOT "
                "marked processed. Retrying in %ds. Touches in cooldown: %s",
                winner["canonical"], winner["side"],
                _FAILED_TOUCH_RETRY_SECONDS,
                sorted(self._retry_cooldown.keys()),
            )

        return record

    # ------------------------------------------------------------------ #
    def _already_isolated_today(self) -> bool:
        doc = self.repo.get_current()
        if not doc:
            return False
        return doc.get("trade_date_ist") == date.today().isoformat()

    def _load_pivot_levels(self) -> dict:
        try:
            db = get_mongo_db()
            doc = db[PROJECT_INFO_COLLECTION].find_one({"_id": PIVOT_DOC_ID})
        except Exception:
            logger.exception("Mongo read failed")
            return {}
        if not doc:
            logger.error("pivot_levels document not found")
            return {}
        return doc.get("indexes") or {}

    def _detect_touches(self, pivot_levels: dict, health: dict) -> list[dict]:
        """Return every touch detected against the current feed snapshot.

        This method intentionally does **not** log each touch — it is
        called on every 30 s scan tick and would otherwise emit a
        duplicate INFO line per active touch per tick. Actionable
        touches are logged by ``scan_once`` after the cooldown filter.
        """
        touches: list[dict] = []
        inst_ltp = health.get("instruments") or {}
        for index_key, payload in pivot_levels.items():
            canonical = payload.get("canonical")
            levels = payload.get("levels") or {}
            ltp = (inst_ltp.get(index_key) or {}).get("ltp")
            if not canonical or ltp is None:
                continue
            touch = self.detector.detect(index_key, ltp, levels)
            if touch:
                touch["canonical"] = canonical
                touch["index_ltp_at_touch"] = ltp
                touch["first_candle"] = payload.get("first_candle")
                touch["or_levels_at_touch"] = levels
                touches.append(touch)
        return touches

    # ------------------------------------------------------------------ #
    # Isolation creation (used by live scan and backfill)                #
    # ------------------------------------------------------------------ #
    def _create_isolation(
        self,
        winner: dict,
        feed: Optional[MarketFeedService],
        *,
        backfilled: bool,
        nifty_ltp_override: float | None = None,
        nifty_candles_override: list[dict] | None = None,
        option_ltp_override: float | None = None,
        option_ltp_source: str | None = None,
        trigger_time_override: str | None = None,
    ) -> Optional[dict]:
        # --- 1. NIFTY LTP ---
        if nifty_ltp_override is not None:
            nifty_ltp = nifty_ltp_override
            nifty_ltp_source = "backfill_candle_close"
        else:
            health = feed.get_health() if feed else {}
            nifty_ltp = (
                (health.get("instruments") or {})
                .get(NIFTY_INDEX_KEY, {})
                .get("ltp")
            )
            nifty_ltp_source = "feed_ltp"
        if nifty_ltp is None:
            logger.warning("NIFTY LTP unavailable; isolation deferred")
            return None

        # --- 2. Trend / bias ---
        if ISOLATION_BIAS_SOURCE == "trigger_side":
            bias = (
                STRATEGY_RELATIONAL_R3_BIAS if winner["side"] == "R3"
                else STRATEGY_RELATIONAL_S3_BIAS
            )
            trend_result = {
                "trend": "TRIGGER_SIDE",
                "bias": bias,
                "reason": f"ISOLATION_BIAS_SOURCE=trigger_side ({winner['side']})",
            }
        else:
            trend_result = self.trend.get_trend(
                nifty_ltp, candles=nifty_candles_override
            )
            bias = trend_result["bias"]

        # --- 3. Strike ---
        strike = compute_atm_strike(nifty_ltp, NIFTY_STRIKE_STEP)

        # --- 4. Search ---
        try:
            instrument = self.search.search_nifty_option_at_strike(
                nifty_ltp, bias, strike=strike,
            )
        except InstrumentSearchError:
            logger.exception("Instrument search failed (bias=%s)", bias)
            return None

        # --- 4a. Resolve the exact opposite option leg (same strike/expiry) ---
        opposite_leg = self._find_opposite_leg(instrument)

        # --- 5. Option LTP snapshot ---
        if option_ltp_override is not None:
            snapshot_ltp = option_ltp_override
            snapshot_source = option_ltp_source or "backfill_candle_close"
        else:
            snapshot_ltp, snapshot_source = self._snapshot_option_ltp(
                feed, instrument.get("instrument_key"),
            )

        # --- 6. Pin execution mode ---
        touch_time = trigger_time_override or winner["detected_at"]
        mode_payload = pin_execution_mode(touch_time, snapshot_ltp)

        # --- 7. Build + persist ---
        record = self._build_record(
            winner=winner,
            instrument=instrument,
            bias=bias,
            strike=strike,
            nifty_ltp=nifty_ltp,
            nifty_ltp_source=nifty_ltp_source,
            trend_result=trend_result,
            option_ltp=snapshot_ltp,
            option_ltp_source=snapshot_source,
            mode_payload=mode_payload,
            touch_time=touch_time,
            backfilled=backfilled,
            opposite_leg=opposite_leg,
        )
        self.repo.upsert_current(record)
        self.repo.append_history(record)
        # Best-effort notification starts only after Mongo persistence.
        # Worker threads do not block this isolation flow waiting for clients.
        notify_isolation_clients(record)
        logger.info(
            "Isolation persisted: %s | mode=%s | bias=%s | backfilled=%s",
            record["trading_symbol"], record["standard_execution_mode"],
            bias, backfilled,
        )
        return record

    # ------------------------------------------------------------------ #
    def _find_opposite_leg(self, instrument: dict) -> dict | None:
        """Resolve the opposite CE/PE contract at the identical strike/expiry."""
        option_type = (instrument.get("instrument_type") or "").upper()
        opposite_type = "PE" if option_type == "CE" else "CE" if option_type == "PE" else None
        strike = instrument.get("strike_price")
        expiry = instrument.get("expiry")
        if not opposite_type or strike is None or not expiry:
            logger.warning(
                "Cannot resolve opposite leg: option_type=%s strike=%s expiry=%s",
                option_type, strike, expiry,
            )
            return None

        try:
            candidates = self.search.search_nifty_option_at_strike_all(
                option_type=opposite_type,
                strike=int(float(strike)),
                expiry=str(expiry),
                exchanges=instrument.get("exchange") or None,
                segments="FO",
                max_records=30,
            )
        except Exception:
            logger.exception(
                "Opposite-leg search failed for %s %s %s",
                strike, opposite_type, expiry,
            )
            return None

        # Do not accept a different expiry/strike even if the search endpoint
        # returns a broad fallback result.
        for candidate in candidates:
            try:
                same_strike = abs(
                    float(candidate.get("strike_price")) - float(strike)
                ) < 1e-6
            except (TypeError, ValueError):
                same_strike = False
            if (
                same_strike
                and (candidate.get("instrument_type") or "").upper() == opposite_type
                and str(candidate.get("expiry")) == str(expiry)
                and candidate.get("instrument_key")
            ):
                return {
                    "instrument_key": candidate.get("instrument_key"),
                    "exchange_token": candidate.get("exchange_token"),
                    "trading_symbol": candidate.get("trading_symbol"),
                    "short_name": candidate.get("short_name"),
                    "name": candidate.get("name"),
                    "strike": candidate.get("strike_price"),
                    "option_type": candidate.get("instrument_type"),
                    "expiry": candidate.get("expiry"),
                    "lot_size": candidate.get("lot_size"),
                    "tick_size": candidate.get("tick_size"),
                    "segment": candidate.get("segment"),
                    "exchange": candidate.get("exchange"),
                    "underlying": (
                        candidate.get("underlying_symbol")
                        or instrument.get("underlying_symbol")
                        or "NIFTY"
                    ),
                }

        logger.warning(
            "No exact opposite leg found: strike=%s expiry=%s type=%s",
            strike, expiry, opposite_type,
        )
        return None

    # ------------------------------------------------------------------ #
    def _snapshot_option_ltp(
        self, feed: Optional[MarketFeedService], instrument_key: Optional[str]
    ) -> tuple[float, str]:
        if not feed or not instrument_key:
            return 0.0, "no_feed"
        feed.subscribe([instrument_key])
        deadline = time.time() + ISOLATION_OPTION_LTP_WAIT_SECONDS
        while time.time() < deadline:
            ltp = feed.get_ltp(instrument_key)
            if ltp is not None:
                logger.info(
                    "Option LTP snapshot: %s = %.2f", instrument_key, ltp
                )
                return ltp, "feed_ltp"
            time.sleep(0.3)
        logger.warning(
            "Option LTP unavailable after %.1fs for %s",
            ISOLATION_OPTION_LTP_WAIT_SECONDS, instrument_key,
        )
        return 0.0, "unavailable"

    # ------------------------------------------------------------------ #
    # Record assembly (with detailed `explanation`)                      #
    # ------------------------------------------------------------------ #
    def _build_record(
        self,
        *,
        winner: dict,
        instrument: dict,
        bias: str,
        strike: int,
        nifty_ltp: float,
        nifty_ltp_source: str,
        trend_result: dict,
        option_ltp: float,
        option_ltp_source: str,
        mode_payload: dict,
        touch_time: str,
        backfilled: bool,
        opposite_leg: dict | None = None,
    ) -> dict:
        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        record = {
            "_id": ISOLATION_DOC_ID,
            "trade_date_ist": date.today().isoformat(),

            # Instrument
            "instrument_key": instrument.get("instrument_key"),
            "exchange_token": instrument.get("exchange_token"),
            "trading_symbol": instrument.get("trading_symbol"),
            "short_name": instrument.get("short_name"),
            "name": instrument.get("name"),
            "strike": instrument.get("strike_price", strike),
            "option_type": instrument.get("instrument_type"),
            "expiry": instrument.get("expiry"),
            "lot_size": instrument.get("lot_size"),
            "tick_size": instrument.get("tick_size"),
            "segment": instrument.get("segment"),
            "exchange": instrument.get("exchange"),
            "underlying": instrument.get("underlying_symbol") or "NIFTY",
            "bias": bias,
            "strike_computed": strike,
            "nifty_ltp_at_isolation": float(nifty_ltp),
            "nifty_ltp_source": nifty_ltp_source,

            # Trigger
            "trigger_index": winner["index_key"],
            "trigger_index_canonical": winner["canonical"],
            "trigger_side": winner["side"],
            "trigger_level": winner["trigger_level"],
            "trigger_level_name": winner["trigger_level_name"],
            "trigger_value": winner["trigger_value"],
            "trigger_priority": winner["priority"],
            "simultaneous_touches": winner.get("simultaneous_touches", []),
            "level_ref": winner.get("level_ref"),

            # Routed reference
            "routed_index": "NIFTY",
            "routed_ltp": float(nifty_ltp),

            # Time
            "isolation_touch_time": touch_time,
            "isolation_created_at": now,

            # Option snapshot
            "isolated_option_ltp": float(option_ltp),
            "isolated_option_ltp_source": option_ltp_source,

            # Mode (immutable)
            **mode_payload,

            # Status / provenance
            "status": "ACTIVE",
            "source": "isolation_service",
            "isolation_backfilled": backfilled,
            "opposite_leg": opposite_leg,
        }
        if backfilled:
            record["backfilled_at"] = now

        record["explanation"] = self._build_explanation(
            winner=winner,
            instrument=instrument,
            bias=bias,
            strike=strike,
            nifty_ltp=nifty_ltp,
            nifty_ltp_source=nifty_ltp_source,
            trend_result=trend_result,
            option_ltp=option_ltp,
            option_ltp_source=option_ltp_source,
            mode_payload=mode_payload,
            touch_time=touch_time,
            backfilled=backfilled,
        )
        return record

    def _build_explanation(
        self,
        *,
        winner: dict,
        instrument: dict,
        bias: str,
        strike: int,
        nifty_ltp: float,
        nifty_ltp_source: str,
        trend_result: dict,
        option_ltp: float,
        option_ltp_source: str,
        mode_payload: dict,
        touch_time: str,
        backfilled: bool,
    ) -> dict:
        or_levels = winner.get("or_levels_at_touch") or {}
        first_candle = winner.get("first_candle") or {}

        return {
            "summary": (
                f"{winner['canonical']} {winner['side']} touch at "
                f"{winner['trigger_level_name']}={winner['trigger_level']:.2f} "
                f"(LTP {winner.get('index_ltp_at_touch', 0):.2f}) "
                f"→ routed to NIFTY @ {nifty_ltp:.2f} → "
                f"trend={trend_result.get('trend')} → bias={bias} → "
                f"strike={strike} → {instrument.get('trading_symbol')}"
            ),
            "backfilled": backfilled,

            "or_levels_source": {
                "first_candle": first_candle,
                "computed_levels": {
                    k: or_levels.get(k)
                    for k in (
                        "R1", "R2", "R3", "R3_Threshold",
                        "S1", "S2", "S3", "S3_Threshold", "A",
                    )
                },
            },

            "trigger_context": {
                "index_key": winner["index_key"],
                "canonical": winner["canonical"],
                "side": winner["side"],
                "level_name": winner["trigger_level_name"],
                "level_value": winner["trigger_level"],
                "index_ltp_at_touch": winner.get("index_ltp_at_touch"),
                "detected_at": touch_time,
                "priority": winner["priority"],
                "simultaneous_touches": winner.get("simultaneous_touches", []),
            },

            "nifty_routing": {
                "nifty_ltp": float(nifty_ltp),
                "source": nifty_ltp_source,
                "bias_source": ISOLATION_BIAS_SOURCE,
            },

            "trend": trend_result,

            "strike_generation": {
                "step": NIFTY_STRIKE_STEP,
                "nifty_ltp": float(nifty_ltp),
                "formula": (
                    f"round({nifty_ltp:.2f} / {NIFTY_STRIKE_STEP}) * "
                    f"{NIFTY_STRIKE_STEP}"
                ),
                "strike": strike,
                "option_type": bias,
            },

            "option_snapshot": {
                "instrument_key": instrument.get("instrument_key"),
                "trading_symbol": instrument.get("trading_symbol"),
                "option_ltp": float(option_ltp),
                "option_ltp_source": option_ltp_source,
            },

            "mode_decision": {
                "standard_execution_mode": mode_payload["standard_execution_mode"],
                "early_extreme_reversal": mode_payload["early_extreme_reversal"],
                "reason": mode_payload.get("execution_mode_reason"),
            },
        }