"""Read-only loader for precomputed Supertrend data of a newly isolated option."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import ISOLATION_SUPERTREND_BACKFILL_DIR, TIMEZONE
from core.logger import get_logger

logger = get_logger(__file__)


def _strike_folder_name(strike: object, option_type: object) -> str:
    """Build the MEGA folder name, for example 25700_CE, from the isolated contract."""
    try:
        numeric_strike = float(strike)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid isolated strike: {strike!r}") from exc

    if numeric_strike <= 0:
        raise ValueError(f"Invalid isolated strike: {strike!r}")

    strike_text = (
        str(int(numeric_strike))
        if numeric_strike.is_integer()
        else format(numeric_strike, "f").rstrip("0").rstrip(".")
    )
    option = str(option_type or "").strip().upper()
    if option not in {"CE", "PE"}:
        raise ValueError(f"Invalid isolated option type: {option_type!r}")
    return f"{strike_text}_{option}"


def load_isolated_supertrend_backfill(record: dict) -> dict:
    """Load the supertrend.json file for the isolated contract.

    The full JSON document is returned under data and tagged as backfill
    data. No signal is emitted, replayed, qualified, or executed here.
    Missing, stale, mismatched, or invalid files are represented by a status
    object so the isolation workflow can continue safely.
    """
    now = datetime.now(ZoneInfo(TIMEZONE))
    instrument_key = str(record.get("instrument_key") or "")
    trade_date = str(record.get("trade_date_ist") or now.date().isoformat())

    result = {
        "status": "not_configured",
        "is_backfill": True,
        "source": "mega_supertrend_json",
        "instrument_key": instrument_key,
        "trade_date_ist": trade_date,
        "loaded_at_ist": now.isoformat(),
        "folder": None,
        "file_path": None,
        "signal_count": 0,
        "data": None,
    }

    if not ISOLATION_SUPERTREND_BACKFILL_DIR:
        logger.info(
            "Supertrend backfill directory not configured | instrument=%s",
            instrument_key,
        )
        return result

    try:
        folder_name = _strike_folder_name(
            record.get("strike"), record.get("option_type")
        )
    except ValueError as exc:
        result.update(status="invalid_instrument", error=str(exc))
        logger.warning(
            "Cannot resolve Supertrend backfill folder | instrument=%s | %s",
            instrument_key, exc,
        )
        return result

    root = Path(ISOLATION_SUPERTREND_BACKFILL_DIR).expanduser()
    folder = (root / folder_name).resolve()
    root_resolved = root.resolve()
    if folder.parent != root_resolved:
        result.update(status="invalid_path", folder=folder_name)
        logger.error("Rejected Supertrend backfill path outside configured root: %s", folder)
        return result

    source = folder / "supertrend.json"
    result.update(folder=folder_name, file_path=str(source))

    if not source.is_file():
        result["status"] = "missing"
        logger.info(
            "Supertrend backfill file not found | instrument=%s | path=%s",
            instrument_key, source,
        )
        return result

    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result.update(status="invalid_json", error=str(exc))
        logger.warning(
            "Could not read Supertrend backfill JSON | instrument=%s | path=%s | error=%s",
            instrument_key, source, exc,
        )
        return result

    if not isinstance(document, dict):
        result.update(status="invalid_format", error="Root JSON value must be an object")
        return result
    if str(document.get("indicator") or "").lower() != "supertrend":
        result.update(status="invalid_format", error="indicator must be 'supertrend'")
        return result
    if document.get("data_scope") != "today_only":
        result.update(status="invalid_scope", error="data_scope must be 'today_only'")
        return result
    if str(document.get("today_ist_date") or "") != trade_date:
        result.update(
            status="stale",
            error=(
                f"today_ist_date={document.get('today_ist_date')!r} "
                f"does not match isolation trade date {trade_date!r}"
            ),
        )
        return result
    document_instrument = str(document.get("instrument_key") or "")
    if document_instrument and instrument_key and document_instrument != instrument_key:
        result.update(
            status="instrument_mismatch",
            error=f"file instrument_key {document_instrument!r} does not match {instrument_key!r}",
        )
        return result

    signals = document.get("signals")
    if not isinstance(signals, list) or any(not isinstance(item, dict) for item in signals):
        result.update(status="invalid_format", error="signals must be a list of objects")
        return result

    result.update(status="loaded", signal_count=len(signals), data=document)
    logger.info(
        "Loaded Supertrend backfill data | instrument=%s | folder=%s | signals=%d | date=%s",
        instrument_key, folder_name, len(signals), trade_date,
    )
    return result
