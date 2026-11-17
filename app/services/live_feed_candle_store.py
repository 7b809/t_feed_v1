# app/services/live_feed_candle_store.py
"""Build and persist one-minute candles directly from live Upstox feed ticks."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe(value: str) -> str:
    return _SAFE_RE.sub("_", str(value).strip()).strip("_") or "instrument"


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tick_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).astimezone(IST)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc).astimezone(IST)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _extract_tick(data: dict[str, Any]) -> dict[str, Any] | None:
    full_feed = data.get("fullFeed") or {}
    if "indexFF" in full_feed:
        feed = full_feed.get("indexFF") or {}
        kind = "index"
    elif "marketFF" in full_feed:
        feed = full_feed.get("marketFF") or {}
        kind = "market"
    else:
        return None

    ltpc = feed.get("ltpc") or {}
    ltp = _number(ltpc.get("ltp"))
    timestamp = _tick_time(ltpc.get("ltt")) or datetime.now(IST)
    if ltp is None:
        return None

    tick = {
        "kind": kind,
        "ts": timestamp,
        "ltp": ltp,
        "cp": _number(ltpc.get("cp")),
        "vtt": _number(feed.get("vtt")) if kind == "market" else None,
        "oi": _number(feed.get("oi")) if kind == "market" else None,
        "iv": _number(feed.get("iv")) if kind == "market" else None,
        "atp": _number(feed.get("atp")) if kind == "market" else None,
        "tbq": _number(feed.get("tbq")) if kind == "market" else None,
        "tsq": _number(feed.get("tsq")) if kind == "market" else None,
        "ltq": _number(ltpc.get("ltq")) if kind == "market" else None,
        "bid": None,
        "ask": None,
        "greeks": None,
    }
    if kind == "market":
        quotes = ((feed.get("marketLevel") or {}).get("bidAskQuote") or [])
        if quotes:
            tick["bid"] = _number(quotes[0].get("bidP"))
            tick["ask"] = _number(quotes[0].get("askP"))
        greeks = feed.get("optionGreeks") or {}
        if greeks:
            tick["greeks"] = {name: _number(value) for name, value in greeks.items()}
    return tick


def _bucket_start(timestamp: datetime) -> datetime:
    return timestamp.replace(second=0, microsecond=0)


class LiveFeedCandleStore:
    """Thread-safe per-instrument daily one-minute candle files.

    Output: <base>/<symbol>_<sanitized-instrument-key>/<YYYY-MM-DD>_1min.json
    The JSON file is atomically replaced after every accepted live tick.
    """

    def __init__(self, config: Settings) -> None:
        self.base_dir = Path(config.live_feed_state_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._cache: dict[str, dict[str, Any]] = {}

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _folder(self, instrument_key: str, symbol: str) -> Path:
        return self.base_dir / f"{_safe(symbol)}_{_safe(instrument_key)}"

    def update_tick(
        self,
        instrument_key: str,
        data: dict[str, Any],
        mode: str,
        instrument_symbol: str,
        isolation_ids: list[str],
        roles: list[str],
    ) -> None:
        tick = _extract_tick(data)
        if tick is None:
            return

        day = tick["ts"].date().isoformat()
        cache_key = f"{instrument_key}|{day}"
        folder = self._folder(instrument_key, instrument_symbol)
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / f"{day}_1min.json"
        lock = self._lock_for(cache_key)

        try:
            with lock:
                state = self._cache.get(cache_key)
                if state is None:
                    if output.exists():
                        try:
                            state = json.loads(output.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            logger.exception("Could not load existing candle file %s", output)
                            state = None
                    if not isinstance(state, dict):
                        state = {
                            "instrument_key": instrument_key,
                            "instrument_symbol": instrument_symbol,
                            "feed_kind": tick["kind"],
                            "interval": "1min",
                            "date": day,
                            "source": "live_feed",
                            "first_tick": tick["ts"].isoformat(),
                            "last_tick": tick["ts"].isoformat(),
                            "tick_count": 0,
                            "candle_count": 0,
                            "last_vtt": None,
                            "candles": [],
                        }
                    self._cache[cache_key] = state

                candles = state.setdefault("candles", [])
                bucket = _bucket_start(tick["ts"])
                bucket_iso = bucket.isoformat()
                candle = next((item for item in candles if item.get("ts") == bucket_iso), None)
                price = float(tick["ltp"])
                if candle is None:
                    candle = {
                        "ts": bucket_iso,
                        "open": round(price, 2),
                        "high": round(price, 2),
                        "low": round(price, 2),
                        "close": round(price, 2),
                        "volume": 0,
                        "ticks": 0,
                    }
                    candles.append(candle)
                    candles.sort(key=lambda item: item.get("ts", ""))

                candle["high"] = round(max(float(candle["high"]), price), 2)
                candle["low"] = round(min(float(candle["low"]), price), 2)
                candle["close"] = round(price, 2)
                candle["ticks"] = int(candle.get("ticks", 0)) + 1

                if tick["vtt"] is not None:
                    previous = _number(state.get("last_vtt"))
                    delta = tick["vtt"] if previous is None else tick["vtt"] - previous
                    candle["volume"] = int(candle.get("volume", 0)) + max(0, int(round(delta)))
                    state["last_vtt"] = tick["vtt"]

                for field in ("cp", "oi", "iv", "atp", "tbq", "tsq", "ltq", "bid", "ask"):
                    if tick.get(field) is not None:
                        candle[field] = tick[field]
                if tick.get("greeks") is not None:
                    candle["greeks"] = tick["greeks"]

                state["instrument_symbol"] = instrument_symbol
                state["feed_kind"] = tick["kind"]
                state["last_tick"] = tick["ts"].isoformat()
                state["tick_count"] = int(state.get("tick_count", 0)) + 1
                state["last_tick_mode"] = mode
                state["isolation_ids"] = sorted(set(isolation_ids))
                state["roles"] = sorted(set(roles))
                if not state.get("first_tick"):
                    state["first_tick"] = tick["ts"].isoformat()
                state["last_tick"] = max(
                    str(state.get("last_tick") or ""), tick["ts"].isoformat()
                )
                state["candle_count"] = len(candles)

                fd, temp_name = tempfile.mkstemp(
                    prefix=f"{day}_1min.", suffix=".tmp", dir=str(folder)
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(state, stream, indent=2, ensure_ascii=False, default=str)
                    Path(temp_name).replace(output)
                except Exception:
                    Path(temp_name).unlink(missing_ok=True)
                    raise
        except Exception:
            logger.exception(
                "Live one-minute candle update failed instrument=%s output=%s",
                instrument_key, output,
            )

    def read_day(self, instrument_key: str, day: str) -> list[dict[str, Any]]:
        """Read persisted one-minute candles for an instrument and IST date."""
        matches = []
        for folder in self.base_dir.glob(f"*_{_safe(instrument_key)}"):
            path = folder / f"{day}_1min.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue
            if payload.get("instrument_key") == instrument_key and isinstance(payload.get("candles"), list):
                matches.extend(item for item in payload["candles"] if isinstance(item, dict))
        unique = {str(item.get("ts")): item for item in matches if item.get("ts")}
        return [unique[key] for key in sorted(unique)]
