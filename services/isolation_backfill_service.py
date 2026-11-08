# services/isolation_backfill_service.py
"""Startup backfill: reconstruct isolation if it would have happened
earlier in the session (Mon–Fri, 09:15–23:59 IST).

Algorithm
---------
1. Window check (weekday + within BACKFILL_WINDOW_* bounds).
2. Abort if today's isolation already exists in Mongo.
3. Load today's OR levels (or compute them).
4. Fetch 1-min intraday candles for all 4 monitored indices.
5. Walk minutes ascending. For each minute compute LTP proxy
   (candle close) and run the same touch detector against OR levels.
6. On the first minute with a touch:
     - build pseudo-winner (highest priority among that minute's touches)
     - fetch NIFTY candles → use the close at that minute as nifty_ltp
     - fetch option candles for the searched instrument → use close at
       that minute as option_ltp (fallback: 0.0 / "unavailable")
     - delegate to IsolationService._create_isolation() with
       backfilled=True and overrides
7. Persist. Log loudly (Telegram not required).
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from core.candle_fetcher import CandleFetcher, CandleFetchError
from core.config import (
    BACKFILL_ENABLED,
    BACKFILL_WINDOW_END_HOUR,
    BACKFILL_WINDOW_END_MINUTE,
    BACKFILL_WINDOW_START_HOUR,
    BACKFILL_WINDOW_START_MINUTE,
    INSTRUMENT_KEYS,
    PIVOT_DOC_ID,
    PROJECT_INFO_COLLECTION,
    TIMEZONE,
)
from core.instrument_search_service import (
    InstrumentSearchError,
    compute_atm_strike,
)
from core.logger import get_logger
from core.mongo_client import get_mongo_db

from services.isolation_service import IsolationService

logger = get_logger(__file__)

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"


class IsolationBackfillService:
    def __init__(self, access_token: str, isolation_service: IsolationService):
        self.access_token = access_token
        self.fetcher = CandleFetcher(access_token)
        self.iso = isolation_service

    # ------------------------------------------------------------------ #
    def backfill_if_needed(self) -> Optional[dict]:
        if not BACKFILL_ENABLED:
            logger.info("Backfill disabled (BACKFILL_ENABLED=false)")
            return None

        now = datetime.now(ZoneInfo(TIMEZONE))
        if not self._within_window(now):
            logger.info(
                "Backfill skipped: %s not within Mon-Fri %02d:%02d–%02d:%02d",
                now.strftime("%A %H:%M"),
                BACKFILL_WINDOW_START_HOUR, BACKFILL_WINDOW_START_MINUTE,
                BACKFILL_WINDOW_END_HOUR, BACKFILL_WINDOW_END_MINUTE,
            )
            return None

        if self._isolation_exists_today():
            logger.info(
                "Backfill skipped: isolation already present for today"
            )
            return None

        pivot_levels = self._load_pivot_levels()
        if not pivot_levels:
            logger.error("Backfill: OR levels unavailable")
            return None

        index_candles = self._fetch_all_index_candles()
        if not index_candles:
            logger.error("Backfill: no index candles available")
            return None

        first_touch = self._find_first_touch(index_candles, pivot_levels)
        if not first_touch:
            logger.info(
                "Backfill: no historical touch found in today's session"
            )
            return None

        logger.warning(
            "Backfill: RECONSTRUCTING isolation — %s/%s touched at %s "
            "(index LTP=%.2f vs level=%.2f)",
            first_touch["winner"]["canonical"],
            first_touch["winner"]["side"],
            first_touch["minute"],
            first_touch["winner"]["index_ltp_at_touch"],
            first_touch["winner"]["trigger_level"],
        )

        # Fetch NIFTY candles for strike + trend
        nifty_candles = index_candles.get(NIFTY_INDEX_KEY) or []
        nifty_ltp_at_minute = self._close_at(nifty_candles, first_touch["minute"])
        if nifty_ltp_at_minute is None:
            logger.error("Backfill: NIFTY close unavailable at %s",
                         first_touch["minute"])
            return None

        # Pre-search the instrument so we can try its candle close too
        bias = self._bias_from_side(first_touch["winner"]["side"])
        strike = compute_atm_strike(nifty_ltp_at_minute)
        try:
            instrument = self.iso.search.search_nifty_option_at_strike(
                nifty_ltp_at_minute, bias, strike=strike,
            )
        except InstrumentSearchError:
            logger.exception(
                "Backfill: search failed for bias=%s strike=%d", bias, strike
            )
            return None

        # Try to fetch option candles for the exact trigger minute
        option_ltp_at_minute: Optional[float] = None
        option_source = "unavailable"
        try:
            opt_candles = self.fetcher.fetch_intraday_candles(
                instrument["instrument_key"]
            )
            option_ltp_at_minute = self._close_at(
                opt_candles, first_touch["minute"]
            )
            if option_ltp_at_minute is not None:
                option_source = "backfill_candle_close"
        except CandleFetchError:
            logger.warning(
                "Backfill: option candles unavailable for %s; "
                "mode will pin to STANDARD",
                instrument.get("instrument_key"),
            )

        record = self.iso._create_isolation(
            winner=first_touch["winner"],
            feed=None,
            backfilled=True,
            nifty_ltp_override=nifty_ltp_at_minute,
            nifty_candles_override=nifty_candles,
            option_ltp_override=(
                option_ltp_at_minute
                if option_ltp_at_minute is not None else 0.0
            ),
            option_ltp_source=option_source,
            trigger_time_override=first_touch["minute"],
        )
        if record:
            logger.warning(
                "Backfill: [MISSED-EARLIER] %s was isolated at %s "
                "| instrument=%s | mode=%s | strike=%s",
                first_touch["winner"]["canonical"],
                first_touch["minute"],
                record["trading_symbol"],
                record["standard_execution_mode"],
                record["strike"],
            )
        return record

    # ------------------------------------------------------------------ #
    def _within_window(self, now: datetime) -> bool:
        if now.weekday() > 4:
            return False
        start = dtime(BACKFILL_WINDOW_START_HOUR, BACKFILL_WINDOW_START_MINUTE)
        end = dtime(BACKFILL_WINDOW_END_HOUR, BACKFILL_WINDOW_END_MINUTE)
        return start <= now.time() <= end

    def _isolation_exists_today(self) -> bool:
        try:
            db = get_mongo_db()
            doc = db[PROJECT_INFO_COLLECTION].find_one(
                {"_id": "instrument_isolation"}
            )
        except Exception:
            logger.exception("Backfill: Mongo read failed")
            return False
        if not doc:
            return False
        from datetime import date
        return doc.get("trade_date_ist") == date.today().isoformat()

    def _load_pivot_levels(self) -> dict:
        try:
            db = get_mongo_db()
            doc = db[PROJECT_INFO_COLLECTION].find_one({"_id": PIVOT_DOC_ID})
        except Exception:
            return {}
        return (doc or {}).get("indexes") or {}

    def _fetch_all_index_candles(self) -> dict:
        result: dict = {}
        for key in INSTRUMENT_KEYS:
            try:
                result[key] = self.fetcher.fetch_intraday_candles(key)
            except CandleFetchError:
                logger.exception("Backfill: candle fetch failed for %s", key)
        return result

    def _find_first_touch(
        self, index_candles: dict, pivot_levels: dict
    ) -> Optional[dict]:
        """Walk minutes ascending; return first minute with any touch."""
        minute_set: set[str] = set()
        for candles in index_candles.values():
            for c in candles:
                minute_set.add(c["timestamp"])

        for minute in sorted(minute_set):
            candidates: list[dict] = []
            for index_key, payload in pivot_levels.items():
                candles = index_candles.get(index_key) or []
                close = self._close_at(candles, minute)
                if close is None:
                    continue
                canonical = payload.get("canonical")
                levels = payload.get("levels") or {}
                touch = self.iso.detector.detect(index_key, close, levels)
                if touch:
                    touch["canonical"] = canonical
                    touch["index_ltp_at_touch"] = close
                    touch["detected_at"] = minute
                    touch["first_candle"] = payload.get("first_candle")
                    touch["or_levels_at_touch"] = levels
                    candidates.append(touch)
            if candidates:
                winner = self.iso.arbitrate(candidates) if hasattr(
                    self.iso, "arbitrate"
                ) else None
                # Use the module-level arbitrate function directly
                from services.touch_arbitration_service import arbitrate_touches
                winner = arbitrate_touches(candidates)
                if winner:
                    return {"minute": minute, "winner": winner}
        return None

    def _close_at(
        self, candles: list[dict], minute_iso: str
    ) -> Optional[float]:
        for c in candles:
            if c["timestamp"] == minute_iso:
                return c["close"]
        return None

    def _bias_from_side(self, side: str) -> str:
        from core.config import (
            STRATEGY_RELATIONAL_R3_BIAS, STRATEGY_RELATIONAL_S3_BIAS,
        )
        return (
            STRATEGY_RELATIONAL_R3_BIAS if side == "R3"
            else STRATEGY_RELATIONAL_S3_BIAS
        )