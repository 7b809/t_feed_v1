"""Capture live Upstox option contracts around each index's OR average.

Contracts are selected from the nearest non-expired expiry and subscribed on
the existing MarketFeedService connection. Each option gets its own daily
JSONL file under data/option_chain/<index-folder>/.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import upstox_client

from core.config import INDEX_METADATA, INSTRUMENT_KEYS, OPTION_CHAIN_INDEXES, TIMEZONE
from core.logger import get_logger
from core.token_service import fetch_access_token
from jobs.daily_refresh import get_feed_service

logger = get_logger(__file__)
IST = ZoneInfo(TIMEZONE)


def _value(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _plain_contract(obj: Any) -> dict:
    if isinstance(obj, dict):
        return dict(obj)
    converter = getattr(obj, "to_dict", None)
    if callable(converter):
        try:
            result = converter()
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    fields = (
        "name", "segment", "exchange", "expiry", "instrument_key",
        "exchange_token", "trading_symbol", "tick_size", "lot_size",
        "instrument_type", "freeze_quantity", "underlying_key",
        "underlying_type", "underlying_symbol", "strike_price",
        "minimum_lot", "weekly",
    )
    return {field: _value(obj, field) for field in fields}


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")[:120]


def select_nearest_expiry_contracts(
    contracts: list[Any],
    today: date,
    average: float,
    range_points: float,
) -> tuple[list[dict], str | None, float, float]:
    """Keep CE/PE contracts at the nearest valid expiry inside the inclusive range."""
    # Zero or negative range captures every strike of the nearest expiry.
    lower, upper = (average - range_points, average + range_points) if range_points > 0 else (None, None)
    valid: list[dict] = []
    for item in contracts or []:
        row = _plain_contract(item)
        try:
            expiry = date.fromisoformat(str(row.get("expiry"))[:10])
            strike = float(row.get("strike_price"))
        except (TypeError, ValueError):
            continue
        option_type = str(row.get("instrument_type") or "").upper()
        key = row.get("instrument_key")
        if expiry < today or not key or option_type not in ("CE", "PE"):
            continue
        row["expiry"] = expiry.isoformat()
        row["strike_price"] = strike
        row["instrument_type"] = option_type
        valid.append(row)

    if not valid:
        return [], None, lower, upper
    nearest_expiry = min(row["expiry"] for row in valid)
    selected = [
        row for row in valid
        if row["expiry"] == nearest_expiry
        and (range_points <= 0 or lower <= row["strike_price"] <= upper)
    ]
    selected.sort(key=lambda row: (row["strike_price"], row["instrument_type"]))
    return selected, nearest_expiry, lower, upper


class OptionChainCaptureService:
    def __init__(self):
        self.enabled = os.getenv("OPTION_CHAIN_CAPTURE_ENABLED", "true").strip().lower() == "true"
        self.index_keys = list(OPTION_CHAIN_INDEXES)
        self.root = Path(os.getenv("OPTION_CHAIN_DATA_DIR", "data/option_chain"))
        self.mode = os.getenv("OPTION_CHAIN_FEED_MODE", "full").strip().lower()
        self.windows = {
            "NIFTY": float(os.getenv("OPTION_CHAIN_NIFTY_RANGE_POINTS", "0")),
            "BANKNIFTY": float(os.getenv("OPTION_CHAIN_BANKNIFTY_RANGE_POINTS", "500")),
            "MIDCPNIFTY": float(os.getenv("OPTION_CHAIN_MIDCPNIFTY_RANGE_POINTS", "200")),
            "SENSEX": float(os.getenv("OPTION_CHAIN_SENSEX_RANGE_POINTS", "500")),
        }
        self._lock = threading.RLock()
        self._feed = None
        self._listener = None
        self._active_date: str | None = None
        self._contract_meta: dict[str, dict] = {}
        self._subscribed: set[str] = set()
        self._owned_subscriptions: set[str] = set()
        self._processed_indexes: set[str] = set()
        self._last_summary: dict = {}
        self._write_queue: queue.Queue = queue.Queue(
            maxsize=max(1000, int(os.getenv("OPTION_CHAIN_WRITE_QUEUE_SIZE", "20000")))
        )
        self._writer_stop = threading.Event()
        self._writer_thread: threading.Thread | None = None
        self._dropped_rows = 0

    def ensure_started(self) -> dict:
        if not self.enabled:
            return {"enabled": False, "state": "disabled"}
        now = datetime.now(IST)
        today = now.date()
        date_key = today.isoformat()
        if today.weekday() >= 5 or not (
            dt_time(9, 15) <= now.time().replace(tzinfo=None) < dt_time(15, 30)
        ):
            return {
                "enabled": True,
                "state": "outside_market_session",
                "trade_date_ist": date_key,
            }
        feed = get_feed_service()
        if feed is None or not feed.get_health().get("running"):
            logger.info("Option-chain capture waiting for the primary market feed")
            return {"enabled": True, "state": "waiting_for_market_feed"}

        with self._lock:
            if (
                self._active_date == date_key
                and self._feed is feed
                and len(self._processed_indexes) == len(self.index_keys)
                and self._contract_meta.keys() <= self._subscribed
                and self._contract_meta.keys() <= set(feed.instrument_keys)
            ):
                return self.status()

        if feed.mode != self.mode:
            logger.error(
                "Option-chain capture mode mismatch: primary feed=%s, configured=%s. "
                "Use OPTION_CHAIN_FEED_MODE=%s or align FEED_MODE.",
                feed.mode, self.mode, feed.mode,
            )
            return {"enabled": True, "state": "feed_mode_mismatch", "feed_mode": feed.mode}

        if not feed.wait_until_connected(2.0):
            logger.info("Option-chain capture waiting for upstream feed connection")
            return {"enabled": True, "state": "waiting_for_connection"}

        with self._lock:
            if self._active_date != date_key:
                self._roll_date(date_key, self._feed or feed)
            if self._feed is not feed:
                if self._feed is not None and self._listener is not None:
                    try:
                        self._feed.remove_message_listener(self._listener)
                    except Exception:
                        logger.exception("Could not detach option-chain listener from old feed")
                self._feed = feed
                # Subscription state belongs to a particular SDK feed object.
                # Rebuild it when daily refresh replaces the primary feed.
                self._subscribed.clear()
                self._owned_subscriptions.clear()
                self._listener = self._on_feed_message
                feed.add_message_listener(self._listener)
            self._ensure_writer()

        try:
            token = fetch_access_token()
            configuration = upstox_client.Configuration()
            configuration.access_token = token
            api = upstox_client.OptionsApi(upstox_client.ApiClient(configuration))
            pivot_doc = self._load_pivot_document(token)
            index_docs = pivot_doc.get("indexes") or {}
            with self._lock:
                all_contracts: list[dict] = list(self._contract_meta.values())
                processed_indexes = set(self._processed_indexes)
            skipped = []

            for instrument_key in self.index_keys:
                if instrument_key in processed_indexes:
                    continue
                meta = INDEX_METADATA.get(instrument_key, {})
                pivot = index_docs.get(instrument_key) or {}
                levels = pivot.get("levels") or {}
                average = levels.get("A", levels.get("avg"))
                if average is None:
                    skipped.append({"index": instrument_key, "reason": "missing_OR_average"})
                    logger.warning("Option-chain skip %s: pivot average A is missing", instrument_key)
                    continue
                try:
                    average = float(average)
                    window = self.windows.get(meta.get("canonical"), 300.0)
                    response = api.get_option_contracts(instrument_key)
                    contracts = _value(response, "data", []) or []
                    selected, nearest_expiry, lower, upper = select_nearest_expiry_contracts(
                        contracts, today, average, window
                    )
                    if not selected:
                        # No result can be transient (provider refresh, expiry
                        # roll, incomplete contracts). Keep this index eligible
                        # for a later low-priority retry.
                        skipped.append({"index": instrument_key, "reason": "no_contracts_in_nearest_expiry_range"})
                        continue
                    for row in selected:
                        row.update({
                            "index_instrument_key": instrument_key,
                            "index_canonical": meta.get("canonical"),
                            "index_folder": meta.get("folder"),
                            "pivot_average": average,
                            "range_points": window,
                            "range_low": lower,
                            "range_high": upper,
                            "selected_expiry": nearest_expiry,
                        })
                    all_contracts.extend(selected)
                    with self._lock:
                        self._processed_indexes.add(instrument_key)
                    range_label = "ALL_STRIKES" if lower is None else f"[{lower:.2f}, {upper:.2f}]"
                    logger.info(
                        "Option-chain selection %s expiry=%s average=%.2f range=%s "
                        "contracts=%d strikes=%d",
                        meta.get("canonical"), nearest_expiry, average, range_label,
                        len(selected), len({r["strike_price"] for r in selected}),
                    )
                except Exception as exc:
                    skipped.append({"index": instrument_key, "reason": f"{type(exc).__name__}: {exc}"})
                    logger.exception("Option-chain contract selection failed for %s", instrument_key)

            if not all_contracts:
                result = {
                    "enabled": True, "state": "no_contracts_selected",
                    "trade_date_ist": date_key, "skipped": skipped,
                }
                with self._lock:
                    self._last_summary = result
                return result

            new_meta = {row["instrument_key"]: row for row in all_contracts}
            for row in new_meta.values():
                row["trade_date_ist"] = date_key
            keys = list(new_meta)
            # Register metadata before sending subscribe requests so an
            # immediate first tick is not lost while the SDK acknowledges.
            with self._lock:
                self._contract_meta.update(new_meta)
            subscribed = set()
            owned_subscriptions = set()
            # Subscribe in bounded chunks so one oversized request does not
            # prevent other selected contracts from being added.
            for offset in range(0, len(keys), 100):
                batch = keys[offset:offset + 100]
                present = set(feed.instrument_keys)
                to_add = [key for key in batch if key not in present]
                if to_add:
                    newly_added = set(feed.subscribe(to_add))
                    subscribed.update(newly_added)
                    owned_subscriptions.update(newly_added)
                subscribed.update(key for key in batch if key in feed.instrument_keys)

            with self._lock:
                self._contract_meta.update(new_meta)
                self._subscribed.update(subscribed)
                self._owned_subscriptions.update(owned_subscriptions)
                self._last_summary = {
                    "enabled": True,
                    "state": "running" if self._contract_meta.keys() <= self._subscribed else "partially_subscribed",
                    "trade_date_ist": date_key,
                    "selected_contracts": len(self._contract_meta),
                    "subscribed_contracts": len(self._subscribed),
                    "skipped_indexes": skipped,
                    "feed_mode": feed.mode,
                }
                self._write_manifests(all_contracts, pivot_doc, skipped)
                summary = dict(self._last_summary)

            try:
                from services.option_chain_supertrend_service import start_option_chain_supertrend_capture
                start_option_chain_supertrend_capture(list(new_meta.values()))
            except Exception:
                logger.exception("Could not start option-chain Supertrend capture")

            logger.info(
                "Option-chain live capture %s: selected=%d subscribed=%d",
                summary["state"], summary["selected_contracts"], summary["subscribed_contracts"],
            )
            return summary
        except Exception:
            logger.exception("Option-chain capture startup failed")
            return {"enabled": True, "state": "error"}

    def _roll_date(self, date_key: str, feed) -> None:
        # Only unsubscribe keys this service added. A selected contract may
        # already be used by the isolation legs or another feed consumer.
        old = set(self._owned_subscriptions)
        if old:
            try:
                feed.unsubscribe(list(old))
            except Exception:
                logger.exception("Could not unsubscribe previous day's option-chain contracts")
        try:
            from services.option_chain_supertrend_service import stop_option_chain_supertrend_capture
            stop_option_chain_supertrend_capture()
        except Exception:
            logger.exception("Could not stop previous-date option-chain Supertrend feeds")
        self._active_date = date_key
        self._contract_meta.clear()
        self._subscribed.clear()
        self._owned_subscriptions.clear()
        self._processed_indexes.clear()
        self._last_summary = {"enabled": True, "state": "preparing", "trade_date_ist": date_key}

    def _load_pivot_document(self, access_token: str) -> dict:
        from core.config import PIVOT_DOC_ID, PROJECT_INFO_COLLECTION
        from core.mongo_client import get_mongo_db
        today = datetime.now(IST).date().isoformat()
        doc = None
        try:
            candidate = get_mongo_db()[PROJECT_INFO_COLLECTION].find_one({"_id": PIVOT_DOC_ID})
            if candidate and candidate.get("trade_date_ist") == today:
                doc = candidate
        except Exception:
            logger.exception("Could not read pivot_levels from Mongo; trying local snapshot")

        if doc is None:
            local = os.getenv("OR_LEVELS_LOCAL_PATH", "data/or_levels.json")
            try:
                with open(local, "r", encoding="utf-8") as handle:
                    candidate = json.load(handle)
                if candidate and candidate.get("trade_date_ist") == today:
                    doc = candidate
            except (OSError, json.JSONDecodeError):
                logger.exception("Could not read local pivot_levels snapshot")

        if doc is None:
            raise RuntimeError("Today's pivot_levels document is not available")

        indexes = doc.get("indexes") or {}
        missing_indexes = [
            key for key in self.index_keys
            if (
                key not in indexes
                or not (indexes.get(key) or {}).get("levels")
                or (indexes.get(key) or {}).get("levels", {}).get(
                    "A", (indexes.get(key) or {}).get("levels", {}).get("avg")
                ) is None
            )
        ]
        if missing_indexes:
            logger.warning(
                "Option-chain capture found missing pivot indexes; requesting OR-level repair: %s",
                missing_indexes,
            )
            try:
                from services.or_levels_service import ORLevelsService
                repaired = ORLevelsService(access_token).ensure_or_levels()
                if repaired and repaired.get("trade_date_ist") == today:
                    doc = repaired
            except Exception:
                logger.exception("Automatic same-day OR-level repair failed")
        return doc

    def _write_manifests(self, contracts: list[dict], pivot_doc: dict, skipped: list) -> None:
        date_key = datetime.now(IST).strftime("%Y%m%d")
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "trade_date_ist": date_key,
            "created_at_ist": datetime.now(IST).isoformat(),
            "feed_mode": self.mode,
            "selected_contracts": len(contracts),
            "skipped_indexes": skipped,
            "indexes": {
                key: {
                    "pivot_average": value.get("pivot_average"),
                    "range_points": value.get("range_points"),
                    "range_low": value.get("range_low"),
                    "range_high": value.get("range_high"),
                    "expiry": value.get("selected_expiry"),
                }
                for key, value in {
                    row["index_instrument_key"]: row for row in contracts
                }.items()
            },
            "contracts": contracts,
            "pivot_levels_computed_at": pivot_doc.get("computed_at"),
        }
        path = self.root / f"option_chain_manifest_{date_key}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)
        for row in contracts:
            folder = self._contract_folder(row, date_key)
            folder.mkdir(parents=True, exist_ok=True)
            metadata_path = folder / "meta.json"
            metadata_tmp = metadata_path.with_suffix(".json.tmp")
            metadata_tmp.write_text(json.dumps(row, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            metadata_tmp.replace(metadata_path)

    def _contract_folder(self, meta: dict, date_key: str) -> Path:
        strike_label = f"{float(meta['strike_price']):g}"
        side = str(meta['instrument_type']).lower()
        folder_name = f"{strike_label}_{side}_{_safe(meta['instrument_key'])}"
        return self.root / str(meta.get('index_folder') or 'unknown') / date_key / folder_name

    def _ensure_writer(self) -> None:
        if self._writer_thread and self._writer_thread.is_alive():
            return
        self._writer_stop.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="option-chain-file-writer",
            daemon=True,
        )
        self._writer_thread.start()

    def _on_feed_message(self, feeds: dict) -> None:
        """Keep the Upstox callback lightweight; disk I/O runs on a worker."""
        received_at = datetime.now(IST).isoformat()
        with self._lock:
            for instrument_key, payload in feeds.items():
                meta = self._contract_meta.get(instrument_key)
                if not meta:
                    continue
                row = {
                    "received_at_ist": received_at,
                    "trade_date_ist": self._active_date,
                    "index_instrument_key": meta.get("index_instrument_key"),
                    "index_canonical": meta.get("index_canonical"),
                    "expiry": meta.get("expiry"),
                    "strike_price": meta.get("strike_price"),
                    "option_type": meta.get("instrument_type"),
                    "instrument_key": instrument_key,
                    "trading_symbol": meta.get("trading_symbol"),
                    "payload": payload,
                }
                try:
                    self._write_queue.put_nowait((meta, instrument_key, row))
                except queue.Full:
                    self._dropped_rows += 1
                    if self._dropped_rows == 1 or self._dropped_rows % 1000 == 0:
                        logger.error(
                            "Option-chain write queue full; dropped_rows=%d",
                            self._dropped_rows,
                        )

    def _writer_loop(self) -> None:
        while not self._writer_stop.is_set() or not self._write_queue.empty():
            try:
                item = self._write_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if item is None:
                    continue
                meta, instrument_key, row = item
                trade_date = str(row.get("trade_date_ist") or datetime.now(IST).date().isoformat())
                date_folder = trade_date.replace("-", "")
                folder = self._contract_folder(meta, date_folder)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / "live_feed.jsonl"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            except Exception:
                logger.exception("Could not write option-chain feed row")
            finally:
                self._write_queue.task_done()

    def stop(self) -> None:
        try:
            from services.option_chain_supertrend_service import stop_option_chain_supertrend_capture
            stop_option_chain_supertrend_capture()
        except Exception:
            logger.exception("Could not stop option-chain Supertrend capture")
        feed = self._feed
        if feed is not None and self._listener is not None:
            try:
                feed.remove_message_listener(self._listener)
            except Exception:
                logger.exception("Could not detach option-chain feed listener")
        self._listener = None
        self._writer_stop.set()
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=5)
        self._writer_thread = None

    def status(self) -> dict:
        with self._lock:
            result = dict(self._last_summary)
        try:
            from services.option_chain_supertrend_service import get_option_chain_supertrend_status
            result['supertrend'] = get_option_chain_supertrend_status()
        except Exception:
            result['supertrend'] = {'state': 'unavailable'}
        return result


option_chain_capture = OptionChainCaptureService()


def ensure_option_chain_capture() -> dict:
    return option_chain_capture.ensure_started()


def get_option_chain_capture_status() -> dict:
    return option_chain_capture.status()


def stop_option_chain_capture() -> None:
    option_chain_capture.stop()
