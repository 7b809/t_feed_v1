"""First-confirmed Supertrend signal selection and sandbox trade-plan calculation.

This module only calculates and archives a proposed plan and sends a Telegram
notification. It never places orders or manages positions.
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from core.logger import get_logger
from services.isolation_telegram_service import send_isolation_html
from services.supertrend_snapshot_cache import get_supertrend_snapshot

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")
_PROCESS_LOCK = threading.RLock()


def _safe(value) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown")).strip("._") or "unknown"


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _normalise_interval(value) -> str:
    text = str(value or "1m").strip().lower()
    return text if text.endswith("m") else f"{text}m"


def _parse_ist_timestamp(value) -> datetime | None:
    """Parse an ISO timestamp; treat naive timestamps as IST."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST)
    except (TypeError, ValueError):
        return None


def _minute_bucket(value) -> datetime | None:
    parsed = _parse_ist_timestamp(value)
    return parsed.replace(second=0, microsecond=0) if parsed else None


def _signal_rows(snapshot: dict, role: str) -> list[dict]:
    rows = snapshot.get("signals") if isinstance(snapshot, dict) else []
    if not isinstance(rows, list):
        return []
    result = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        signal = str(item.get("signal") or "").strip().lower()
        confirmed_time = item.get("confirmed_time")
        if signal not in {"buy", "sell"} or not confirmed_time:
            continue
        row = dict(item)
        row["signal"] = signal
        row["source_leg_role"] = role
        result.append(row)
    return result


class SandboxSupertrendSignalService:
    """Select one earliest confirmed signal per interval and calculate a plan."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("ISOLATED_DATA_DIR", "data/isolated"))
        self.output_root = self.root / "sandbox_services"
        self.enabled = os.getenv("SANDBOX_SUPERTREND_SIGNALS_ENABLED", "true").strip().lower() == "true"
        self._status: dict[str, dict] = {}

    def process_update(
        self,
        isolation_record: dict,
        interval: str,
        ltp_provider: Callable | None,
    ) -> dict:
        """Evaluate the two cached snapshots after any accepted WS frame."""
        if not self.enabled:
            return {"state": "disabled", "interval": _normalise_interval(interval)}
        interval = _normalise_interval(interval)
        trade_date = str(isolation_record.get("trade_date_ist") or datetime.now(IST).date().isoformat())
        legs = self._legs(isolation_record)
        if any(not (legs.get(role) or {}).get("instrument_key") for role in ("isolated", "opposite")):
            return {"state": "waiting_for_both_legs", "interval": interval}

        snapshots = {}
        for role, leg in legs.items():
            result = get_supertrend_snapshot(
                leg["instrument_key"], trade_date_ist=trade_date, interval=interval
            )
            if result.get("status") != "loaded" or not isinstance(result.get("data"), dict):
                return {
                    "state": "waiting_for_snapshot",
                    "interval": interval,
                    "missing_leg": role,
                }
            snapshots[role] = result["data"]

        return self.process_snapshots(
            isolation_record, interval, snapshots, ltp_provider=ltp_provider
        )

    def process_snapshots(
        self,
        isolation_record: dict,
        interval: str,
        snapshots: dict,
        ltp_provider: Callable | None,
    ) -> dict:
        """Pure-ish entry point used by the WS integration and deterministic tests."""
        interval = _normalise_interval(interval)
        trade_date = str(isolation_record.get("trade_date_ist") or datetime.now(IST).date().isoformat())
        isolation_id = str(
            isolation_record.get("isolation_id")
            or f"{trade_date}__{isolation_record.get('instrument_key') or 'unknown'}"
        )
        output_path = self._output_path(trade_date, isolation_id, interval)

        with _PROCESS_LOCK:
            existing = self._load_json(output_path)
            if existing:
                state = {"state": "already_processed", "interval": interval, "file": str(output_path)}
                self._status[f"{isolation_id}|{interval}"] = state
                return state

            legs = self._legs(isolation_record)
            if set(snapshots) < {"isolated", "opposite"}:
                return {"state": "waiting_for_both_snapshots", "interval": interval}

            isolation_time = (
                isolation_record.get("isolation_created_at")
                or isolation_record.get("isolation_time_ist")
                or isolation_record.get("isolation_touch_time")
            )
            isolation_dt = _parse_ist_timestamp(isolation_time)
            isolation_minute = _minute_bucket(isolation_time)
            if isolation_minute is None:
                state = {
                    "state": "waiting_for_isolation_timestamp",
                    "interval": interval,
                    "reason": "The isolation record has no valid IST timestamp.",
                }
                self._status[f"{isolation_id}|{interval}"] = state
                return state

            pending_path = output_path.with_suffix(".pending.json")
            pending_document = self._load_json(pending_path)

            if pending_document:
                # The first eligible signal is locked even if live LTP is not
                # available yet. Later signals cannot replace this selection.
                first_signal = pending_document["first_signal"]
                signal_selection = pending_document["signal_selection"]
            else:
                eligible_candidates = []
                rejected_candidates = []
                for role in ("isolated", "opposite"):
                    snap = snapshots.get(role) or {}
                    expected_key = legs[role]["instrument_key"]
                    actual_key = str(snap.get("instrument_key") or expected_key)
                    if actual_key != expected_key:
                        continue
                    actual_interval = _normalise_interval(snap.get("interval") or interval)
                    if actual_interval != interval:
                        continue
                    for signal in _signal_rows(snap, role):
                        confirmed_dt = _parse_ist_timestamp(signal.get("confirmed_time"))
                        if confirmed_dt is None:
                            continue
                        received_dt = _parse_ist_timestamp(
                            signal.get("received_at_ist") or snap.get("snapshot_updated_at_ist")
                        ) or confirmed_dt
                        signal_minute = confirmed_dt.replace(second=0, microsecond=0)
                        row = {
                            **signal,
                            "confirmed_at_ist": confirmed_dt.isoformat(),
                            "received_at_ist": received_dt.isoformat(),
                            "source_leg_role": role,
                            "signal_minute_ist": signal_minute.isoformat(),
                            "eligible_for_isolation": signal_minute >= isolation_minute,
                            "eligibility_reason": (
                                "Confirmed in the same minute as isolation or in a later minute."
                                if signal_minute >= isolation_minute
                                else "Confirmed before the isolation minute."
                            ),
                        }
                        if row["eligible_for_isolation"]:
                            eligible_candidates.append(row)
                        else:
                            rejected_candidates.append(row)

                if not eligible_candidates:
                    state = {
                        "state": "waiting_for_first_signal",
                        "interval": interval,
                        "isolation_time_ist": isolation_dt.isoformat(),
                        "eligible_signal_count": 0,
                        "ignored_pre_isolation_signal_count": len(rejected_candidates),
                        "ignored_signals": rejected_candidates,
                    }
                    self._status[f"{isolation_id}|{interval}"] = state
                    return state

                # Earliest confirmation wins. If confirmation times match,
                # the first WebSocket arrival wins. An exact tie prefers the
                # isolated leg for deterministic behaviour.
                eligible_candidates.sort(
                    key=lambda item: (
                        _parse_ist_timestamp(item.get("confirmed_time")) or datetime.max.replace(tzinfo=IST),
                        _parse_ist_timestamp(item.get("received_at_ist")) or datetime.max.replace(tzinfo=IST),
                        0 if item.get("source_leg_role") == "isolated" else 1,
                    )
                )
                first_signal = eligible_candidates[0]
                signal_selection = {
                    "rule": (
                        "Signals confirmed in the same minute as isolation or later are eligible. "
                        "Seconds within the isolation minute do not disqualify a signal. "
                        "The earliest confirmation wins; equal confirmation times are ordered "
                        "by WebSocket arrival time. The first selected signal is locked."
                    ),
                    "isolation_time_ist": isolation_dt.isoformat(),
                    "isolation_minute_ist": isolation_minute.isoformat(),
                    "eligible_signal_count": len(eligible_candidates),
                    "ignored_pre_isolation_signal_count": len(rejected_candidates),
                    "eligible_signals_considered": eligible_candidates,
                    "ignored_signals": rejected_candidates,
                    "selected_because": (
                        "This was the first eligible signal by confirmation time. "
                        "If another leg confirmed at the same time, this signal's WebSocket "
                        "frame arrived first. The selection is now locked for this interval."
                    ),
                }
                self._atomic_write(pending_path, {
                    "isolation_id": isolation_id,
                    "interval": interval,
                    "first_signal": first_signal,
                    "signal_selection": signal_selection,
                    "locked_at_ist": datetime.now(IST).isoformat(),
                })

            source_role = first_signal["source_leg_role"]
            target_role = source_role if first_signal["signal"] == "buy" else (
                "opposite" if source_role == "isolated" else "isolated"
            )
            target_leg = dict(legs[target_role])
            target_key = target_leg["instrument_key"]

            live_quote = self._get_live_quote(ltp_provider, target_key)
            entry = _number(live_quote.get("ltp"))
            if entry is None or entry <= 0 or live_quote.get("stale") is True:
                state = {
                    "state": "waiting_for_live_entry_price",
                    "interval": interval,
                    "target_leg_role": target_role,
                    "target_instrument_key": target_key,
                    "quote": live_quote,
                }
                self._status[f"{isolation_id}|{interval}"] = state
                return state

            pivot_doc = self._load_pivot_levels(trade_date, target_role, target_key, interval)
            plan = self.calculate_plan(
                entry=entry,
                signal=first_signal,
                source_leg_role=source_role,
                target_leg=target_leg,
                target_role=target_role,
                pivot_doc=pivot_doc,
                live_quote=live_quote,
                interval=interval,
            )
            document = {
                "schema_version": 1,
                "document_type": "sandbox_supertrend_signal_plan",
                "trade_date_ist": trade_date,
                "isolation_id": isolation_id,
                "created_at_ist": datetime.now(IST).isoformat(),
                "interval": interval,
                "first_signal": first_signal,
                "signal_selection": signal_selection,
                "signal_source": {
                    "leg_role": source_role,
                    "instrument_key": legs[source_role]["instrument_key"],
                    "trading_symbol": legs[source_role].get("trading_symbol"),
                },
                "target": {
                    **target_leg,
                    "leg_role": target_role,
                    "entry_price": entry,
                    "entry_price_source": "live_market_feed",
                    "entry_price_received_at_ist": live_quote.get("received_at"),
                },
                "calculation": plan,
                "decision": {
                    "rule": (
                        "BUY signal targets the instrument that generated the signal; "
                        "SELL signal targets the opposite leg."
                    ),
                    "reason": plan["decision_reason"],
                },
                "source_snapshots": {
                    role: {
                        "instrument_key": legs[role]["instrument_key"],
                        "interval": interval,
                        "signal_count": len(_signal_rows(snapshots.get(role) or {}, role)),
                        "snapshot_updated_at_ist": (
                            snapshots.get(role) or {}
                        ).get("snapshot_updated_at_ist"),
                    }
                    for role in ("isolated", "opposite")
                },
                "execution": {
                    "mode": "sandbox_only",
                    "order_placed": False,
                    "position_created": False,
                },
            }
            self._atomic_write(output_path, document)
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove pending signal lock file: %s", pending_path)
            notification_sent = self._notify(document)
            document["notification"] = {
                "attempted": True,
                "delivered_to_at_least_one_destination": notification_sent,
            }
            # Rewrite so the archive also contains notification outcome.
            self._atomic_write(output_path, document)
            state = {
                "state": "processed",
                "interval": interval,
                "file": str(output_path),
                "target_instrument_key": target_key,
                "notification_sent": notification_sent,
            }
            self._status[f"{isolation_id}|{interval}"] = state
            logger.info(
                "Sandbox Supertrend plan saved | isolation=%s | interval=%s | "
                "source=%s/%s | signal=%s | target=%s | entry=%.2f | SL=%.2f | target_price=%.2f",
                isolation_id, interval, source_role, legs[source_role]["instrument_key"],
                first_signal["signal"], target_key, entry,
                plan["stop_loss"], plan["target_price"],
            )
            return state

    @staticmethod
    def _legs(record: dict) -> dict:
        isolated = {
            "leg_role": "isolated",
            "instrument_key": str(record.get("instrument_key") or ""),
            "trading_symbol": record.get("trading_symbol") or record.get("instrument_key"),
            "option_type": record.get("option_type"),
            "strike": record.get("strike"),
            "expiry": record.get("expiry"),
        }
        opposite_source = record.get("opposite_leg") or {}
        opposite = {
            "leg_role": "opposite",
            "instrument_key": str(opposite_source.get("instrument_key") or ""),
            "trading_symbol": opposite_source.get("trading_symbol") or opposite_source.get("instrument_key"),
            "option_type": opposite_source.get("option_type"),
            "strike": opposite_source.get("strike", opposite_source.get("strike_price")),
            "expiry": opposite_source.get("expiry"),
        }
        return {
            "isolated": isolated,
            "opposite": opposite,
        }

    def _output_path(self, trade_date: str, isolation_id: str, interval: str) -> Path:
        return self.output_root / trade_date.replace("-", "") / (
            f"{_safe(isolation_id)}_{_safe(interval)}.json"
        )

    @staticmethod
    def _get_live_quote(provider: Callable | None, instrument_key: str) -> dict:
        if provider is None:
            return {"ltp": None, "stale": True, "reason": "live_price_provider_unavailable"}
        try:
            value = provider(instrument_key)
            if isinstance(value, dict):
                return value
            return {"ltp": value, "stale": value is None}
        except Exception as exc:
            logger.exception("Live LTP lookup failed for %s", instrument_key)
            return {"ltp": None, "stale": True, "reason": str(exc)}

    def _load_pivot_levels(self, trade_date: str, role: str, key: str, interval: str) -> dict:
        interval_number = interval.removesuffix("m")
        path = (
            self.root / "pivots" / trade_date.replace("-", "") / role
            / _safe(key) / f"pivot_levels_{interval_number}m.json"
        )
        doc = self._load_json(path)
        if not doc:
            return {"available": False, "path": str(path), "levels": {}}
        return {
            "available": True,
            "path": str(path),
            "source_candle_timestamp": doc.get("source_candle_timestamp"),
            "levels": doc.get("levels") or {},
        }

    @staticmethod
    def calculate_plan(
        entry: float,
        signal: dict,
        source_leg_role: str,
        target_leg: dict,
        target_role: str,
        pivot_doc: dict,
        live_quote: dict,
        interval: str,
    ) -> dict:
        percentage_stop = entry - (entry * 0.30)
        levels = pivot_doc.get("levels") or {}
        supports = []
        for name in ("S1", "S2", "S3"):
            value = _number(levels.get(name))
            if value is not None and 0 < value < entry:
                supports.append({"level": name, "value": value})
        nearest_support = max(supports, key=lambda item: item["value"]) if supports else None

        if nearest_support is None:
            stop_loss = percentage_stop
            stop_basis = "30_percent_fallback_no_valid_support_below_entry"
        elif percentage_stop <= nearest_support["value"]:
            stop_loss = percentage_stop
            stop_basis = "30_percent_stop_is_lower_than_nearest_support"
        else:
            stop_loss = nearest_support["value"]
            stop_basis = "nearest_support_is_lower_than_30_percent_stop"

        if entry <= 100:
            target_offset = 40.0
            target_band = "entry <= 100"
        elif entry <= 200:
            target_offset = 50.0
            target_band = "100 < entry <= 200"
        else:
            target_offset = 60.0
            target_band = "entry > 200"
        target_price = entry + target_offset

        signal_word = "bullish" if signal["signal"] == "buy" else "bearish"
        if signal["signal"] == "buy":
            decision_reason = (
                f"The first {interval} {signal_word} Supertrend signal came from the "
                f"{source_leg_role} leg, so that same instrument is selected as the target."
            )
        else:
            decision_reason = (
                f"The first {interval} {signal_word} Supertrend signal came from the "
                f"{source_leg_role} leg, so the opposite ({target_role}) instrument is selected."
            )

        return {
            "entry_price": entry,
            "entry_basis": "latest non-stale live LTP of the selected target instrument",
            "percentage_stop_loss": round(percentage_stop, 4),
            "support_candidates_below_entry": supports,
            "nearest_support": nearest_support,
            "support_pivot_source": pivot_doc.get("path"),
            "support_pivot_candle_timestamp": pivot_doc.get("source_candle_timestamp"),
            "stop_loss": round(stop_loss, 4),
            "stop_loss_basis": stop_basis,
            "target_offset": target_offset,
            "target_band": target_band,
            "target_price": round(target_price, 4),
            "signal_confirmation_price": _number(signal.get("confirmed_time_price")),
            "signal_confirmed_time": signal.get("confirmed_time"),
            "signal_type": signal.get("signal"),
            "signal_interval": interval,
            "source_leg_role": source_leg_role,
            "target_leg_role": target_role,
            "live_quote_age_seconds": live_quote.get("age_seconds"),
            "decision_reason": decision_reason,
            "formula_summary": (
                "SL = min(entry - 30% of entry, nearest valid S1/S2/S3 below entry); "
                "if no support exists, use the 30% stop. "
                "Target = entry + 40/50/60 according to entry-price band."
            ),
        }

    def _notify(self, document: dict) -> bool:
        signal = document["first_signal"]
        target = document["target"]
        calc = document["calculation"]
        selection = document.get("signal_selection") or {}
        source = document["signal_source"]
        signal_word = "Bullish (BUY)" if signal["signal"] == "buy" else "Bearish (SELL)"
        target_reason = (
            "A bullish signal selects the same instrument that gave the signal."
            if signal["signal"] == "buy"
            else "A bearish signal selects the opposite instrument."
        )
        same_minute = (
            _minute_bucket(signal.get("confirmed_time"))
            == _minute_bucket(selection.get("isolation_time_ist"))
        )
        eligibility = (
            "The signal was confirmed in the same minute as isolation. "
            "We accept it even when its seconds are earlier than the isolation seconds."
            if same_minute
            else "The signal was confirmed after the isolation minute."
        )
        nearest = calc.get("nearest_support")
        support_text = (
            f"{nearest.get('level')} at {self._fmt(nearest.get('value'))}"
            if nearest else "No valid support below entry; the 30% stop is used."
        )
        ignored_count = int(selection.get("ignored_pre_isolation_signal_count") or 0)
        other_count = max(0, int(selection.get("eligible_signal_count") or 1) - 1)
        stop_basis = calc.get("stop_loss_basis")
        if stop_basis == "30_percent_fallback_no_valid_support_below_entry":
            stop_explanation = "No support level was below entry, so we used the 30% stop."
        elif stop_basis == "30_percent_stop_is_lower_than_nearest_support":
            stop_explanation = "The 30% stop was lower than the nearest support, so we used the 30% stop."
        else:
            stop_explanation = "The nearest support was lower than the 30% stop, so we used that support."
        if calc.get("target_offset") == 40:
            target_explanation = "Entry is ₹100 or less, so the target is entry + ₹40."
        elif calc.get("target_offset") == 50:
            target_explanation = "Entry is above ₹100 and up to ₹200, so the target is entry + ₹50."
        else:
            target_explanation = "Entry is above ₹200, so the target is entry + ₹60."
        opposite = document.get("source_snapshots", {}).get("opposite", {})
        isolated = document.get("source_snapshots", {}).get("isolated", {})
        message = "\n".join([
            "🧪 <b>New Supertrend Signal Plan</b>",
            "",
            f"<b>Interval:</b> {html.escape(document['interval'])}",
            f"<b>Isolation time:</b> <code>{html.escape(str(selection.get('isolation_time_ist') or '—'))}</code>",
            f"<b>Signal time:</b> <code>{html.escape(str(signal.get('confirmed_time') or '—'))}</code>",
            f"<b>Signal:</b> <b>{html.escape(signal_word)}</b>",
            f"<b>Confirmation price:</b> {self._fmt(signal.get('confirmed_time_price'))}",
            f"<b>Signal came from:</b> {html.escape(str(source.get('trading_symbol') or source.get('instrument_key') or '—'))}",
            f"<b>Isolated instrument:</b> <code>{html.escape(str(isolated.get('instrument_key') or '—'))}</code>",
            f"<b>Opposite instrument:</b> <code>{html.escape(str(opposite.get('instrument_key') or '—'))}</code>",
            f"<b>Why accepted:</b> {html.escape(eligibility)}",
            f"<b>Other signals:</b> {other_count} other eligible signal(s) were checked; {ignored_count} signal(s) from before the isolation minute were ignored.",
            "",
            "<b>Why this instrument was selected</b>",
            html.escape(target_reason),
            f"Selected leg: <b>{html.escape(str(target.get('leg_role') or '—').title())}</b>",
            f"Trading symbol: <b>{html.escape(str(target.get('trading_symbol') or '—'))}</b>",
            f"Instrument key: <code>{html.escape(str(target.get('instrument_key') or '—'))}</code>",
            "",
            "<b>Price plan</b>",
            f"Live entry price: <b>{self._fmt(target.get('entry_price'))}</b>",
            f"Stop loss: <b>{self._fmt(calc.get('stop_loss'))}</b>",
            f"Target price: <b>{self._fmt(calc.get('target_price'))}</b>",
            "",
            "<b>How the stop was calculated</b>",
            f"30% stop: {self._fmt(calc.get('percentage_stop_loss'))}",
            f"Nearest support below entry: {html.escape(support_text)}",
            f"<b>Stop decision:</b> {html.escape(stop_explanation)}",
            "",
            "<b>How the target was calculated</b>",
            f"Entry band: {html.escape(str(calc.get('target_band') or '—'))}",
            f"Target increase: +{self._fmt(calc.get('target_offset'))}",
            html.escape(target_explanation),
            "",
            "<b>In simple words</b>",
            "We checked the first Supertrend signal from both option instruments. "
            "We kept the first eligible signal and used its direction to choose the target instrument. "
            "Entry uses the selected instrument's live market price.",
            "",
            "<i>This is a saved calculation and notification only. No order was placed.</i>",
        ])
        return send_isolation_html(message)

    @staticmethod
    def _fmt(value) -> str:
        number = _number(value)
        return "—" if number is None else f"{number:,.2f}"

    @staticmethod
    def _load_json(path: Path) -> dict | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _atomic_write(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="sandbox_signal_", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False, allow_nan=False, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def status(self) -> dict:
        return {"enabled": self.enabled, "output_directory": str(self.output_root), "intervals": dict(self._status)}


_service = SandboxSupertrendSignalService()


def process_supertrend_signal_update(
    isolation_record: dict,
    interval: str,
    ltp_provider: Callable | None,
) -> dict:
    return _service.process_update(isolation_record, interval, ltp_provider)


def get_sandbox_signal_status() -> dict:
    return _service.status()
