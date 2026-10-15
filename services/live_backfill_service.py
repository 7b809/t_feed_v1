"""Seed the live EMA engine from backup + intraday candles at startup.

Problem solved
--------------
On a mid-day restart, the EMA engine starts with an empty candle
buffer. This means:

    * every cross that happened earlier in the session is lost;
    * the engine needs ``EMA_SLOW_PERIOD + 2`` fresh minutes before it
      can detect a cross again (~23 minutes by default).

This service:

    1. Reads ``candles.json`` from the extracted backup for each
       registry instrument (covers up to yesterday's close).
    2. Fetches today's intraday 1-minute candles from Upstox.
    3. Merges the two into a single ascending series.
    4. Feeds the merged series into ``LiveEmaEngine``.
    5. Asks the engine to scan the seeded series for crosses that
       occurred after today's 00:00, so the mid-day gap is filled
       without re-emitting crosses already recorded in the backup.

The service is a no-op when the registry is empty. It runs before
``live_feed_service.start()`` so the seeded buffer is contiguous with
the first live tick. Any exception is logged and swallowed — a failed
backfill must never block startup.

Logging behaviour
-----------------
``_VERBOSE_PER_INSTRUMENT`` controls the log level:

    True   ->  per-instrument stage lines (backup, intraday, merge,
               ema_seed, cross_scan, instrument:DONE) plus periodic
               PROGRESS lines. Useful for debugging.

    False  ->  only the aggregate START line, the aggregate COMPLETE
               line, and any per-instrument error (FAILED) lines.
               Recommended for normal operation.

Regardless of the flag, per-instrument errors are always logged at
ERROR/WARNING level so that a partially-failed backfill is never
silent.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.live_ema_engine import live_ema_engine
from services.token_service import token_service

logger = get_logger(__file__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

_UPSTOX_BASE = "https://api.upstox.com"
_HTTP_TIMEOUT = 20
_BACKUP_TAIL = 500            # keep this many most-recent backup candles per instrument
_MAX_FILE_BYTES = 64 * 1024 * 1024   # refuse to read backup files larger than 64 MB

# Per-instrument stage logs ("backup:START", "intraday:DONE", ...) plus
# periodic PROGRESS lines. Set to False in production for quiet operation;
# errors are still logged regardless of this flag.
_VERBOSE_PER_INSTRUMENT = False

# Emit an aggregate PROGRESS line after every N completed instruments.
# Only honoured when ``_VERBOSE_PER_INSTRUMENT`` is True.
_PROGRESS_EVERY = 25

# ---------------------------------------------------------------------------
# Timestamp / candle normalisation
# ---------------------------------------------------------------------------

def _parse_ts_to_iso(value: Any) -> str | None:
    """Convert many timestamp shapes into a local-tz ISO string."""
    if value is None or value == "":
        return None

    # numeric epoch (seconds / millis / micros)
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.replace(".", "", 1).lstrip("-").isdigit()
    ):
        try:
            n = float(value)
            if n > 1e15:
                n /= 1e6
            elif n > 1e12:
                n /= 1e3
            dt = datetime.fromtimestamp(n, tz=timezone.utc).astimezone()
            return dt.replace(second=0, microsecond=0).isoformat()
        except (OSError, OverflowError, ValueError):
            return None

    # ISO 8601 (with or without tz)
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone()
        return dt.replace(second=0, microsecond=0).isoformat()
    except ValueError:
        pass

    # a couple of common non-ISO patterns
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).astimezone()
            return dt.replace(second=0, microsecond=0).isoformat()
        except ValueError:
            continue

    return None

def _to_engine_candle(raw: Any) -> dict[str, Any] | None:
    """Normalise a single candle from any common shape.

    Accepted:
        [ts, o, h, l, c, v]
        {"timestamp"|"time"|"ts": ..., "open"|"o": ..., ...}
    """
    if raw is None:
        return None

    ts_raw: Any = None
    o = h = l = c = v = None

    if isinstance(raw, (list, tuple)) and len(raw) >= 5:
        ts_raw, o, h, l, c = raw[0], raw[1], raw[2], raw[3], raw[4]
        if len(raw) >= 6:
            v = raw[5]
    elif isinstance(raw, dict):
        ts_raw = raw.get("timestamp") or raw.get("time") or raw.get("ts")
        o = raw.get("open", raw.get("o"))
        h = raw.get("high", raw.get("h"))
        l = raw.get("low", raw.get("l"))
        c = raw.get("close", raw.get("c"))
        v = raw.get("volume", raw.get("v", raw.get("vol")))
    else:
        return None

    try:
        o = float(o); h = float(h); l = float(l); c = float(c)
    except (TypeError, ValueError):
        return None

    ts_iso = _parse_ts_to_iso(ts_raw)
    if ts_iso is None:
        return None

    try:
        v = float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        v = 0.0

    return {
        "minute": ts_iso[:16],           # "YYYY-MM-DDTHH:MM"
        "timestamp": ts_iso,
        "open": o, "high": h, "low": l, "close": c,
        "volume": v,
    }

def _extract_candles_array(data: Any) -> list[Any]:
    """Walk the common nested shapes to find the candle list."""
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("candles"), list):
            return inner["candles"]
        if isinstance(data.get("candles"), list):
            return data["candles"]
        if isinstance(inner, list):
            return inner
    return []

# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------

def _read_backup_candles(entry: dict[str, Any], tail: int) -> list[dict[str, Any]]:
    path_str = entry.get("candles_path") or ""
    if not path_str:
        return []
    path = Path(path_str)
    if not path.is_file():
        return []

    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            logger.warning(
                "Backfill: %s too large (%d bytes); skipping",
                path, path.stat().st_size,
            )
            return []
    except OSError:
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Backfill: unreadable %s", path)
        return []

    raw_list = _extract_candles_array(payload)
    if not raw_list:
        return []
    if tail and len(raw_list) > tail:
        raw_list = raw_list[-tail:]

    out: list[dict[str, Any]] = []
    for raw in raw_list:
        c = _to_engine_candle(raw)
        if c is not None:
            out.append(c)
    return out

def _fetch_intraday(instrument_key: str) -> list[dict[str, Any]]:
    token = token_service.get_access_token()
    if not token:
        return []

    url = f"{_UPSTOX_BASE}/v3/historical-candle/intraday/{instrument_key}/minutes/1"
    try:
        resp = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException:
        logger.warning("Backfill: intraday request failed for %s", instrument_key)
        return []

    if resp.status_code >= 400:
        logger.warning(
            "Backfill: intraday HTTP %s for %s: %s",
            resp.status_code, instrument_key, resp.text[:180],
        )
        return []

    try:
        payload = resp.json()
    except ValueError:
        return []

    out: list[dict[str, Any]] = []
    for raw in _extract_candles_array(payload):
        c = _to_engine_candle(raw)
        if c is not None:
            out.append(c)
    return out

def _merge_candles(
    backup: list[dict[str, Any]],
    intraday: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Dedupe by minute key; intraday wins on conflict."""
    merged: dict[str, dict[str, Any]] = {}
    for c in backup:
        merged[c["minute"]] = c
    for c in intraday:
        merged[c["minute"]] = c
    return sorted(merged.values(), key=lambda c: c["minute"])

def _current_minute_key() -> str:
    return datetime.now().astimezone().replace(
        second=0, microsecond=0
    ).isoformat()[:16]

def _short(key: str) -> str:
    """Compact instrument label for log lines."""
    s = str(key or "")
    return s if len(s) <= 40 else s[:20] + "…" + s[-16:]

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class LiveBackfillService:
    """Seeds the EMA engine from backup + intraday candles at startup."""

    def seed_all(self) -> dict[str, Any]:
        t_overall = time.monotonic()

        entries = instrument_registry.all()
        if not entries:
            logger.info("[backfill] no instruments in registry — skipping")
            return {"seeded": 0, "crosses": 0, "skipped": 0, "errors": 0, "total": 0}

        today_date = datetime.now().astimezone().date().isoformat()
        today_prefix = today_date + "T00:00"   # only scan today's candles
        now_minute = _current_minute_key()

        total = len(entries)
        workers = max(1, min(config.CHART_MAX_BATCH_WORKERS, 8))

        logger.info(
            "[backfill] START — instruments=%d workers=%d today=%s backup_tail=%d verbose=%s",
            total, workers, today_date, _BACKUP_TAIL, _VERBOSE_PER_INSTRUMENT,
        )

        seeded = 0
        skipped = 0
        errors = 0
        total_crosses = 0
        completed_counter = 0
        progress_lock = threading.Lock()

        def _vlog(msg: str, *args: Any) -> None:
            """Per-instrument stage log; suppressed unless verbose."""
            if _VERBOSE_PER_INSTRUMENT:
                logger.info(msg, *args)

        def _seed_one(idx: int, entry: dict[str, Any]) -> tuple[int, int, bool]:
            key = str(entry.get("instrument_key") or "")
            label = _short(key)
            if not key:
                return 0, 0, False

            t_instr = time.monotonic()
            _vlog("[backfill %d/%d] %s — instrument:START", idx, total, label)

            # ---- Stage 1: backup candles ---------------------------------
            _vlog("[backfill %d/%d] %s — backup:START", idx, total, label)
            try:
                backup = _read_backup_candles(entry, _BACKUP_TAIL)
            except Exception:
                logger.exception(
                    "[backfill %d/%d] %s — backup:FAILED", idx, total, label
                )
                backup = []
            _vlog(
                "[backfill %d/%d] %s — backup:DONE candles=%d",
                idx, total, label, len(backup),
            )

            # ---- Stage 2: intraday candles -------------------------------
            _vlog("[backfill %d/%d] %s — intraday:START", idx, total, label)
            try:
                intraday = _fetch_intraday(key)
            except Exception:
                logger.exception(
                    "[backfill %d/%d] %s — intraday:FAILED", idx, total, label
                )
                intraday = []
            _vlog(
                "[backfill %d/%d] %s — intraday:DONE candles=%d",
                idx, total, label, len(intraday),
            )

            # Drop the in-progress current-minute candle so the live feed
            # owns it cleanly (avoids double-appending).
            intraday = [c for c in intraday if c["minute"] < now_minute]

            # ---- Stage 3: merge ------------------------------------------
            _vlog("[backfill %d/%d] %s — merge:START", idx, total, label)
            try:
                merged = _merge_candles(backup, intraday)
            except Exception:
                logger.exception(
                    "[backfill %d/%d] %s — merge:FAILED", idx, total, label
                )
                merged = []
            _vlog(
                "[backfill %d/%d] %s — merge:DONE candles=%d",
                idx, total, label, len(merged),
            )

            if not merged:
                _vlog(
                    "[backfill %d/%d] %s — SKIP (no candles after merge)",
                    idx, total, label,
                )
                return 0, 0, False

            # ---- Stage 4: seed EMA engine --------------------------------
            _vlog("[backfill %d/%d] %s — ema_seed:START", idx, total, label)
            try:
                live_ema_engine.seed(key, merged)
            except Exception:
                logger.exception(
                    "[backfill %d/%d] %s — ema_seed:FAILED", idx, total, label
                )
                return 0, 0, False
            _vlog(
                "[backfill %d/%d] %s — ema_seed:DONE candles=%d",
                idx, total, label, len(merged),
            )

            # ---- Stage 5: scan for crosses since today's open ------------
            _vlog(
                "[backfill %d/%d] %s — cross_scan:START since=%s",
                idx, total, label, today_prefix,
            )
            try:
                crosses = live_ema_engine.scan_crosses(
                    key, since_minute_prefix=today_prefix,
                )
            except Exception:
                logger.exception(
                    "[backfill %d/%d] %s — cross_scan:FAILED", idx, total, label
                )
                crosses = 0
            _vlog(
                "[backfill %d/%d] %s — cross_scan:DONE crosses=%d",
                idx, total, label, crosses,
            )

            elapsed = time.monotonic() - t_instr
            _vlog(
                "[backfill %d/%d] %s — instrument:DONE (%.2fs) "
                "backup=%d intraday=%d merged=%d crosses=%d",
                idx, total, label, elapsed,
                len(backup), len(intraday), len(merged), crosses,
            )
            return len(merged), crosses, True

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_seed_one, idx, entry): entry
                for idx, entry in enumerate(entries, start=1)
            }
            for fut in as_completed(futures):
                entry = futures[fut]
                label = _short(entry.get("instrument_key") or "?")
                try:
                    n_candles, n_crosses, ok = fut.result()
                except Exception:
                    errors += 1
                    logger.exception(
                        "[backfill] %s — instrument:FAILED (unhandled)", label
                    )
                    continue

                if ok:
                    seeded += 1
                    total_crosses += n_crosses
                else:
                    skipped += 1

                with progress_lock:
                    completed_counter += 1
                    if (
                        _VERBOSE_PER_INSTRUMENT
                        and (completed_counter % _PROGRESS_EVERY == 0
                             or completed_counter == total)
                    ):
                        logger.info(
                            "[backfill] PROGRESS %d/%d — seeded=%d skipped=%d errors=%d crosses=%d",
                            completed_counter, total,
                            seeded, skipped, errors, total_crosses,
                        )

        elapsed_overall = time.monotonic() - t_overall
        summary = {
            "seeded": seeded,
            "skipped": skipped,
            "crosses": total_crosses,
            "errors": errors,
            "total": total,
            "today": today_date,
            "elapsed_seconds": round(elapsed_overall, 2),
        }
        logger.info(
            "[backfill] COMPLETE — seeded=%d skipped=%d errors=%d crosses=%d "
            "total=%d today=%s elapsed=%.2fs",
            seeded, skipped, errors, total_crosses, total,
            today_date, elapsed_overall,
        )
        return summary

live_backfill_service = LiveBackfillService()