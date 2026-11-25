"""Prompt-driven AlgoCLI test endpoint with per-request local artifacts.

Live GTT writes are disabled unless both the request and server configuration
explicitly authorize them. The API token supplied by callers is intentionally
ignored; AlgoCLI owns its own authentication and credentials.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from clients.algocli_client import AlgoCLIClient, AlgoCLIError, AlgoCLIWriteAmbiguous
from core.config import ALGOCLI_TEST_DATA_DIR
from core.logger import get_logger

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")


def _redact(value: Any) -> Any:
    """Remove credential-like fields before writing any test artifact."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            name = str(key).lower()
            if any(token in name for token in (
                "api_token", "access_token", "refresh_token", "authorization",
                "api_key", "password", "secret", "bearer",
            )):
                continue
            cleaned[str(key)] = _redact(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return re.sub(
            r"(?i)(bearer\s+|(?:api|access|refresh)[_ -]?token\s*[:=]\s*)[^\s,;]+",
            r"\1[REDACTED]", value,
        )
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_redact(value), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _safe_operation_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:64]
    return cleaned or uuid.uuid4().hex


class AlgoCLITestService:
    """Build, optionally execute, and persist one AlgoCLI test request."""

    def __init__(self, client: AlgoCLIClient | None = None):
        self.client = client or AlgoCLIClient()
        self.root = Path(ALGOCLI_TEST_DATA_DIR).expanduser()

    def place_gtt(
        self,
        *,
        instrument_key: str,
        trading_symbol: str,
        target: float,
        stoploss: float,
        entry: float | None = None,
        lots: int = 1,
        confirm_live_order: bool = False,
        live_writes_enabled: bool = False,
        operation_id: str | None = None,
    ) -> dict:
        operation_id = _safe_operation_id(operation_id or uuid.uuid4().hex)
        now = datetime.now(IST)
        timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
        folder_name = f"{timestamp}_{operation_id}"
        output_dir = self.root / folder_name

        # The current place_gtt prompt intentionally supports immediate market
        # entry only. Refuse unsupported limit/trigger-entry semantics.
        if entry is not None:
            raise ValueError("Only entry=null (immediate market entry) is supported by this test API.")
        if not instrument_key.strip() or not trading_symbol.strip():
            raise ValueError("instrument_key and trading_symbol are required.")
        if lots < 1:
            raise ValueError("lots must be at least 1.")
        if stoploss <= 0 or target <= 0 or stoploss >= target:
            raise ValueError("stoploss and target must be positive, with stoploss below target.")

        values = {
            "instrument_key": instrument_key.strip(),
            "trading_symbol": trading_symbol.strip(),
            "lots": int(lots),
            "lot_label": f"{int(lots)} lot" if int(lots) == 1 else f"{int(lots)} lots",
            "entry_instruction": "Enter immediately at the current live market price; do not set an entry trigger price.",
            "sl_price": float(stoploss),
            "target_price": float(target),
            "operation_id": operation_id,
        }
        prompt = self.client._template("test_place_gtt.txt", values)
        request = {
            "operation_id": operation_id,
            "instrument_key": values["instrument_key"],
            "trading_symbol": values["trading_symbol"],
            "entry": None,
            "target": values["target_price"],
            "stoploss": values["sl_price"],
            "lots": values["lots"],
            "confirm_live_order": bool(confirm_live_order),
        }

        # Persist intent before invoking the CLI. If the process stops while a
        # broker write is in flight, the prompt and request remain auditable.
        output_dir.mkdir(parents=True, exist_ok=True)
        initial_metadata = {
            "operation_id": operation_id,
            "created_at_ist": now.isoformat(),
            "status": "RUNNING" if confirm_live_order and live_writes_enabled else "PREPARED",
            "prompt_template": "prompts/algocli/test_place_gtt.txt",
            "artifact_files": ["request.json", "prompt.txt", "response.json", "metadata.json"],
        }
        _write_json(output_dir / "request.json", request)
        (output_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
        _write_json(output_dir / "response.json", {
            "status": initial_metadata["status"],
            "executed": "UNKNOWN" if initial_metadata["status"] == "RUNNING" else False,
        })
        _write_json(output_dir / "metadata.json", initial_metadata)

        if confirm_live_order and not live_writes_enabled:
            response = {
                "status": "BLOCKED",
                "executed": False,
                "message": "Live GTT writes are disabled by ALGOCLI_TEST_LIVE_WRITES_ENABLED.",
            }
            outcome = "BLOCKED"
        elif not confirm_live_order:
            response = {
                "status": "DRY_RUN",
                "executed": False,
                "message": "Prompt generated only. Set confirm_live_order=true and enable the server-side live-write gate to execute.",
            }
            outcome = "DRY_RUN"
        else:
            try:
                result = self.client._run("test_place_gtt.txt", values, write=True)
                response = _redact(result)
                outcome = "COMPLETED"
            except AlgoCLIWriteAmbiguous as exc:
                response = {
                    "status": "AMBIGUOUS",
                    "executed": "UNKNOWN",
                    "error": str(exc),
                    "algocli_execution": _redact(self.client.last_execution),
                    "warning": "Do not retry automatically. Reconcile broker GTT state first.",
                }
                outcome = "AMBIGUOUS"
            except AlgoCLIError as exc:
                response = {
                    "status": "FAILED",
                    "executed": False,
                    "error": str(exc),
                    "algocli_execution": _redact(self.client.last_execution),
                }
                outcome = "FAILED"
            except Exception as exc:
                logger.exception("Unexpected AlgoCLI test execution failure | operation=%s", operation_id)
                response = {
                    "status": "ERROR",
                    "executed": "UNKNOWN",
                    "error": str(exc),
                    "algocli_execution": _redact(self.client.last_execution),
                }
                outcome = "ERROR"

        metadata = {
            "operation_id": operation_id,
            "created_at_ist": now.isoformat(),
            "status": outcome,
            "prompt_template": "prompts/algocli/test_place_gtt.txt",
            "cli_execution": _redact(self.client.last_execution),
            "artifact_files": ["request.json", "prompt.txt", "response.json", "metadata.json"],
        }
        _write_json(output_dir / "response.json", response)
        _write_json(output_dir / "metadata.json", metadata)

        return {
            "status": outcome,
            "operation_id": operation_id,
            "executed": response.get("executed", outcome == "COMPLETED"),
            "artifact_directory": str(output_dir.as_posix()),
            "files": metadata["artifact_files"],
            "response": response,
        }


    def get_positions_health(self) -> dict:
        """Execute the read-only get_positions prompt and archive the health check."""
        operation_id = uuid.uuid4().hex
        started_at = datetime.now(IST)
        started_monotonic = __import__("time").monotonic()
        output_dir = self.root / (
            f"{started_at.strftime('%Y%m%d_%H%M%S_%f')}_get-positions_{operation_id[:8]}"
        )
        template_name = "get_positions.txt"
        prompt_path = "prompts/algocli/get_positions.txt"
        output_dir.mkdir(parents=True, exist_ok=True)

        request = {
            "operation_id": operation_id,
            "method": "GET",
            "endpoint": "/api/algocli-test/get-positions",
            "prompt_template": prompt_path,
            "read_only": True,
        }
        _write_json(output_dir / "request.json", request)
        (output_dir / "prompt.txt").write_text(
            self.client._template(template_name, {}) + "\n",
            encoding="utf-8",
        )
        _write_json(output_dir / "response.json", {
            "status": "RUNNING",
            "operation_id": operation_id,
        })

        response: dict[str, Any]
        outcome = "UNHEALTHY"
        try:
            # Use _run directly so the API returns the full parsed AlgoAI
            # response and execution metadata, rather than only a positions
            # list extracted by the convenience client method.
            result = self.client._run(template_name, {"operation_id": operation_id})
            response = _redact(result)
            outcome = "HEALTHY"
        except Exception as exc:
            logger.exception("AlgoAI health check execution failed | operation=%s", operation_id)
            response = {
                "error": _redact(str(exc)),
                "cli_execution": _redact(getattr(self.client, "last_execution", {})),
            }

        duration = round(__import__("time").monotonic() - started_monotonic, 3)
        completed_at = datetime.now(IST)
        execution = _redact(getattr(self.client, "last_execution", {}))
        metadata = {
            "operation_id": operation_id,
            "status": outcome,
            "prompt_template": prompt_path,
            "started_at_ist": started_at.isoformat(),
            "completed_at_ist": completed_at.isoformat(),
            "duration_seconds": duration,
            "cli_execution": execution,
            "artifact_files": ["request.json", "prompt.txt", "response.json", "metadata.json"],
        }
        archived_response = {
            "status": outcome,
            "operation_id": operation_id,
            "healthy": outcome == "HEALTHY",
            "duration_seconds": duration,
            "response": response,
        }
        _write_json(output_dir / "response.json", archived_response)
        _write_json(output_dir / "metadata.json", metadata)

        logger.info(
            "AlgoAI get-positions health check %s | operation=%s | duration=%.3fs | cli_outcome=%s",
            outcome, operation_id, duration, execution.get("outcome", "UNKNOWN"),
        )
        return {
            **archived_response,
            "artifact_directory": output_dir.as_posix(),
            "files": metadata["artifact_files"],
        }
