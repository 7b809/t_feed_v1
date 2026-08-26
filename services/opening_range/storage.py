"""
Opening Range JSON storage helpers.

This module handles:

1. Saving touch-event debug data when test mode is enabled.
2. Saving the complete Opening Range calculation summary.
3. Atomic JSON file replacement to reduce partially written files.

All runtime data is read through state.py snapshots. This module does
not access old monolithic global variables directly.
"""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from core.logger import get_logger

from . import state as runtime_state
from .candle_utils import (
    get_live_ema_calculation_mode_text,
    get_now_market_time,
)
from .constants import (
    DEFAULT_LIVE_EMA_CALCULATION_MODE,
    DEFAULT_OUTPUT_FILE,
    DEFAULT_TEST_FLAG,
    DEFAULT_TOUCH_EVENTS_OUTPUT_FILE,
    DEFAULT_TOUCH_EVENTS_SAVE_TEST_FILE,
)

logger = get_logger(__file__)


# ============================================================
# Internal JSON Helpers
# ============================================================


def _normalize_output_path(
    output_file: str | Path,
    default_output_file: str,
) -> Path:
    """
    Normalizes and validates an output file path.

    The configured default path is used when output_file is empty.
    """
    if output_file is None:
        output_file = default_output_file

    normalized_text = str(output_file).strip()

    if not normalized_text:
        normalized_text = default_output_file

    return Path(normalized_text)


def _json_default_serializer(
    value: Any,
) -> str:
    """
    Serializes values that are not directly supported by json.dump().

    This primarily supports:

        datetime
        date
        Path
        Decimal
        SDK-specific values

    Unsupported values are converted to strings to preserve the
    original behavior of default=str.
    """
    if isinstance(value, Path):
        return str(value)

    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass

    return str(value)


def _write_json_atomically(
    payload: Any,
    file_path: Path,
) -> str:
    """
    Writes JSON through a temporary file and atomically replaces the
    destination.

    This reduces the chance of leaving a partially written JSON file
    when the process stops during a write.
    """
    file_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_file_path: Path | None = None

    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(file_path.parent),
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file_path = Path(temporary_file.name)

            json.dump(
                payload,
                temporary_file,
                indent=4,
                ensure_ascii=False,
                default=_json_default_serializer,
            )

            temporary_file.write("\n")
            temporary_file.flush()

            try:
                os.fsync(temporary_file.fileno())
            except OSError:
                # Some environments or filesystems may not support
                # fsync. The JSON file can still be replaced safely.
                pass

        temporary_file_path.replace(file_path)

        return str(file_path)

    except Exception:
        if temporary_file_path is not None and temporary_file_path.exists():
            try:
                temporary_file_path.unlink()
            except OSError:
                pass

        raise


# ============================================================
# Touch Event Test Storage
# ============================================================


def save_touch_events_to_file_if_enabled() -> str | None:
    """
    Saves touch events and isolated EMA alerts to a test JSON file.

    The file is saved only when both conditions are true:

        TEST_FLAG = True

        OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE = True

    Returns:

        Output file path after successful write.

        None when storage is disabled or the write fails.
    """
    if not DEFAULT_TEST_FLAG:
        return None

    if not DEFAULT_TOUCH_EVENTS_SAVE_TEST_FILE:
        return None

    runtime_state.ensure_current_market_day()

    try:
        file_path = _normalize_output_path(
            output_file=DEFAULT_TOUCH_EVENTS_OUTPUT_FILE,
            default_output_file=("data/opening_range_touch_events.json"),
        )

        touch_snapshot = runtime_state.get_touch_state_snapshot()

        isolated_state = runtime_state.get_selected_or_state_snapshot()

        isolated_ema_alerts = runtime_state.get_selected_or_ema_alerts_snapshot()

        generated_at = get_now_market_time()

        payload = {
            "generated_at": generated_at.isoformat(),
            "date": generated_at.date().isoformat(),
            "live_ema_calculation_mode_flag": (DEFAULT_LIVE_EMA_CALCULATION_MODE),
            "live_ema_calculation_mode": (get_live_ema_calculation_mode_text()),
            "events_count": touch_snapshot.get(
                "events_count",
                0,
            ),
            "events": touch_snapshot.get(
                "events",
                [],
            ),
            "pending_events_count": (
                touch_snapshot.get(
                    "pending_events_count",
                    0,
                )
            ),
            "pending_events": touch_snapshot.get(
                "pending_events",
                [],
            ),
            "alert_sent_keys_count": (
                touch_snapshot.get(
                    "alert_sent_keys_count",
                    0,
                )
            ),
            "isolated_instrument": isolated_state,
            "isolated_ema_alerts_count": len(isolated_ema_alerts),
            "isolated_ema_alerts": isolated_ema_alerts,
            "latest_main_index_ltp": (touch_snapshot.get("latest_main_index_ltp")),
            "latest_main_index_ltp_source": (
                touch_snapshot.get("latest_main_index_ltp_source")
            ),
            "latest_main_index_ltp_updated_at": (
                touch_snapshot.get("latest_main_index_ltp_updated_at")
            ),
        }

        saved_path = _write_json_atomically(
            payload=payload,
            file_path=file_path,
        )

        logger.info(
            "Opening Range touch-event test data saved. "
            "file_path=%s, events_count=%s, "
            "isolated_ema_alerts_count=%s",
            saved_path,
            payload["events_count"],
            payload["isolated_ema_alerts_count"],
        )

        return saved_path

    except Exception as ex:
        logger.exception(
            "Failed saving Opening Range touch events. " "error=%s: %s",
            type(ex).__name__,
            ex,
        )
        return None


# ============================================================
# Opening Range Result Storage
# ============================================================


def save_opening_range_results_to_file(
    summary: dict,
    output_file: str = DEFAULT_OUTPUT_FILE,
) -> str:
    """
    Saves an Opening Range summary to a JSON file.

    The destination file is replaced atomically after the complete JSON
    payload has been written to a temporary file.

    Raises:

        TypeError:
            When summary is not a dictionary.

        OSError:
            When the destination cannot be created or written.

        ValueError:
            When JSON serialization fails.
    """
    if not isinstance(summary, dict):
        raise TypeError("Opening Range summary must be a dictionary.")

    file_path = _normalize_output_path(
        output_file=output_file,
        default_output_file=DEFAULT_OUTPUT_FILE,
    )

    saved_path = _write_json_atomically(
        payload=summary,
        file_path=file_path,
    )

    logger.info(
        "Opening Range results saved. " "file_path=%s, status=%s, total_instruments=%s",
        saved_path,
        summary.get("status"),
        summary.get("total_instruments"),
    )

    return saved_path


# ============================================================
# Public API
# ============================================================


__all__ = [
    "save_touch_events_to_file_if_enabled",
    "save_opening_range_results_to_file",
]
