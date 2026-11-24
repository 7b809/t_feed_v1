"""Subprocess adapter for the independent AlgoCLI installation.

The isolation service owns strategy decisions and never imports the Upstox SDK
for order execution. AlgoCLI owns its own authentication and broker access.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path
from string import Template
from typing import Any

from core.config import (
    ALGOCLI_LAUNCHER_PATH,
    ALGOCLI_PROJECT_DIR,
    ALGOCLI_PYTHON,
    ALGOCLI_TIMEOUT_SECONDS,
    ALGOCLI_SKIP_PERMISSIONS,
)
from core.logger import get_logger

logger = get_logger(__file__)


def _redact_text(value: str) -> str:
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", value)
    value = re.sub(
        r"(?i)((?:upstox_)?access[_ -]?token|authorization|api[_ -]?key|password|secret)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]", value,
    )
    return value

class AlgoCLIError(RuntimeError):
    """AlgoCLI failed before a trustworthy structured response was available."""


class AlgoCLIWriteAmbiguous(AlgoCLIError):
    """A write prompt may have reached the broker; never retry automatically."""


class AlgoCLIClient:
    """Execute predefined prompts through the independent AlgoCLI launcher."""

    def __init__(self) -> None:
        self.project_dir = Path(ALGOCLI_PROJECT_DIR).expanduser() if ALGOCLI_PROJECT_DIR else None
        launcher = Path(ALGOCLI_LAUNCHER_PATH).expanduser()
        if not launcher.is_absolute() and self.project_dir:
            launcher = self.project_dir / launcher
        self.launcher = launcher
        self.last_execution: dict[str, Any] = {}

    def _template(self, name: str, values: dict[str, Any]) -> str:
        root = Path(__file__).resolve().parents[1] / "prompts" / "algocli"
        path = root / name
        if not path.is_file():
            raise AlgoCLIError(f"AlgoCLI prompt template is missing: {name}")
        template = Template(path.read_text(encoding="utf-8"))
        try:
            return template.substitute({k: str(v) for k, v in values.items()})
        except KeyError as exc:
            raise AlgoCLIError(f"Missing prompt value for {exc.args[0]} in {name}") from exc

    @staticmethod
    def _json_from_stdout(stdout: str) -> Any:
        """Extract the last valid JSON value from CLI output (timing lines may surround it)."""
        decoder = json.JSONDecoder()
        candidates = []
        for index, char in enumerate(stdout):
            if char not in "{[":
                continue
            try:
                value, end = decoder.raw_decode(stdout[index:])
                candidates.append((index + end, value))
            except json.JSONDecodeError:
                continue
        if not candidates:
            raise AlgoCLIError("AlgoCLI did not return valid JSON on stdout")
        return max(candidates, key=lambda item: item[0])[1]

    @staticmethod
    def _unwrap(value: Any) -> Any:
        """Unwrap common CLI JSON envelopes without inventing broker fields."""
        current = value
        for _ in range(4):
            if isinstance(current, str):
                try:
                    current = json.loads(current)
                    continue
                except json.JSONDecodeError:
                    return value
            if isinstance(current, dict):
                next_value = None
                for key in ("response", "result", "output", "content", "text"):
                    candidate = current.get(key)
                    if isinstance(candidate, (dict, list)):
                        next_value = candidate
                        break
                    if isinstance(candidate, str):
                        try:
                            next_value = json.loads(candidate)
                            break
                        except json.JSONDecodeError:
                            continue
                if next_value is not None:
                    current = next_value
                    continue
            break
        return current

    def _run(self, template_name: str, values: dict[str, Any], *, write: bool = False) -> dict:
        if not self.project_dir or not self.project_dir.is_dir():
            raise AlgoCLIError(
                "ALGOCLI_PROJECT_DIR must point to the independent AlgoCLI project; "
                "execution is blocked until it is configured."
            )
        if not self.launcher.is_file():
            raise AlgoCLIError(f"AlgoCLI launcher does not exist: {self.launcher}")

        operation_id = str(values.get("operation_id") or uuid.uuid4().hex)
        prompt_values = {**values, "operation_id": operation_id}
        prompt = self._template(template_name, prompt_values)
        command = [
            ALGOCLI_PYTHON, str(self.launcher),
            "--output-format", "json",
            "--prompt", prompt,
        ]
        if ALGOCLI_SKIP_PERMISSIONS:
            command.append("--skip-permissions")

        started = time.monotonic()
        proc = subprocess.Popen(
            command,
            cwd=str(self.project_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name != "nt"),
        )
        try:
            stdout, stderr = proc.communicate(timeout=max(10, ALGOCLI_TIMEOUT_SECONDS))
        except subprocess.TimeoutExpired as exc:
            if os.name != "nt":
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
                proc.communicate()
            message = f"AlgoCLI timed out after {ALGOCLI_TIMEOUT_SECONDS}s; broker outcome is unknown"
            self.last_execution = {
                "operation_id": operation_id, "template": template_name,
                "prompt": prompt, "exit_code": None,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stderr": str(exc), "outcome": "AMBIGUOUS" if write else "FAILED",
            }
            if write:
                raise AlgoCLIWriteAmbiguous(message) from exc
            raise AlgoCLIError(message) from exc

        duration = round(time.monotonic() - started, 3)
        self.last_execution = {
            "operation_id": operation_id, "template": template_name,
            "prompt": prompt, "exit_code": proc.returncode,
            "duration_seconds": duration,
            "stderr": _redact_text(stderr[-4000:]), "outcome": "COMPLETED" if proc.returncode == 0 else ("AMBIGUOUS" if write else "FAILED"),
        }
        if proc.returncode != 0:
            message = _redact_text(f"AlgoCLI exited with code {proc.returncode}: {stderr[-1500:] or stdout[-1500:]}")
            if write:
                raise AlgoCLIWriteAmbiguous(
                    message + " (write outcome is unknown; reconcile before any retry)"
                )
            raise AlgoCLIError(message)

        try:
            parsed = self._json_from_stdout(stdout)
            payload = self._unwrap(parsed)
            if not isinstance(payload, (dict, list)):
                raise AlgoCLIError("AlgoCLI JSON response must be an object or array")
        except AlgoCLIError as exc:
            if write:
                self.last_execution["outcome"] = "AMBIGUOUS"
                raise AlgoCLIWriteAmbiguous(
                    f"{exc}; AlgoCLI exited successfully but the broker response is unreadable. Reconcile before retrying."
                ) from exc
            raise
        return {
            "response": payload,
            "_algocli": {
                "operation_id": operation_id,
                "template": template_name,
                "prompt": prompt,
                "exit_code": proc.returncode,
                "duration_seconds": duration,
            },
        }

    @staticmethod
    def _data(result: dict) -> Any:
        value = result.get("response", result)
        for _ in range(4):
            if isinstance(value, dict):
                for key in ("data", "result", "orders", "positions"):
                    candidate = value.get(key)
                    if isinstance(candidate, (dict, list)):
                        value = candidate
                        break
                else:
                    break
            else:
                break
        return value

    def get_ltp(self, instrument_key: str) -> dict:
        result = self._run("get_ltp.txt", {"instrument_key": instrument_key})
        data = self._data(result)
        if isinstance(data, dict) and instrument_key in data and isinstance(data[instrument_key], dict):
            data = data[instrument_key]
        if isinstance(data, list):
            data = next((x for x in data if isinstance(x, dict) and
                         (x.get("instrument_key") == instrument_key or x.get("instrument_token") == instrument_key)), {})
        if not isinstance(data, dict):
            raise AlgoCLIError("LTP response did not contain an object")
        price = data.get("last_price", data.get("ltp"))
        if price is None:
            raise AlgoCLIError(f"LTP response has no last_price for {instrument_key}")
        return {"last_price": float(price), "provider_timestamp": data.get("timestamp"),
                "raw": result}

    def place_gtt_bracket(self, instrument_key, quantity, product, entry, stop, target, operation_id=None):
        return self._run("place_gtt.txt", {
            "instrument_key": instrument_key, "quantity": int(quantity), "product": product,
            "entry_price": entry, "sl_price": stop, "target_price": target,
            "operation_id": operation_id,
        }, write=True)

    def get_gtt(self, gtt_id: str) -> dict:
        result = self._run("get_gtt.txt", {"gtt_order_id": gtt_id})
        data = self._data(result)
        if isinstance(data, list):
            matches = [x for x in data if isinstance(x, dict) and
                       str(x.get("gtt_order_id") or x.get("id") or "") == gtt_id]
            if len(matches) != 1:
                raise AlgoCLIError(f"Expected one GTT record for {gtt_id}; received {len(matches)}")
            return matches[0]
        if not isinstance(data, dict):
            raise AlgoCLIError("GTT detail response is not an object")
        return data

    def cancel_gtt(self, gtt_id: str):
        return self._run("cancel_gtt.txt", {"gtt_order_id": gtt_id}, write=True)

    def modify_gtt_stop(self, gtt_id: str, stop: float):
        result = self._run("modify_gtt_stop.txt", {
            "gtt_order_id": gtt_id, "stop_price": stop,
        }, write=True)
        write_meta = result.get("_algocli", {})
        try:
            verified = self.get_gtt(gtt_id)
            rules = verified.get("rules")
            matches = [
                rule for rule in (rules if isinstance(rules, list) else [])
                if isinstance(rule, dict) and str(rule.get("strategy", "")).upper() == "STOPLOSS"
            ]
            if len(matches) != 1:
                raise AlgoCLIError("Could not verify exactly one STOPLOSS rule after AlgoCLI modification")
            actual = matches[0].get("trigger_price")
            try:
                matches_requested = abs(float(actual) - float(stop)) <= 0.011
            except (TypeError, ValueError):
                matches_requested = False
            if not matches_requested:
                raise AlgoCLIError(
                    f"Runner stop modification is not verified: requested {stop}, broker reports {actual}"
                )
        except Exception as exc:
            self.last_execution = {
                "operation_id": write_meta.get("operation_id"),
                "template": write_meta.get("template"),
                "prompt": write_meta.get("prompt"),
                "outcome": "AMBIGUOUS",
                "verification_error": _redact_text(str(exc)),
            }
            raise AlgoCLIWriteAmbiguous(
                "GTT stop modification may have been submitted, but broker verification failed; reconcile before retrying."
            ) from exc
        self.last_execution = write_meta
        return {
            "modify_response": result.get("response"),
            "verified_gtt": verified,
            "_algocli": write_meta,
        }

    def get_positions(self) -> list[dict]:
        result = self._run("get_positions.txt", {})
        data = self._data(result)
        if isinstance(data, dict):
            data = data.get("positions", data.get("net", data.get("data", [])))
        if not isinstance(data, list):
            raise AlgoCLIError("Positions response is not an array")
        return data

    def place_market_exit(self, instrument_key, quantity, product, tag):
        return self._run("place_market_exit.txt", {
            "instrument_key": instrument_key, "quantity": int(quantity),
            "product": product, "tag": tag,
        }, write=True)

    def get_order_details(self, order_id):
        result = self._run("get_order_details.txt", {"order_id": order_id})
        data = self._data(result)
        if isinstance(data, list):
            if not data:
                raise AlgoCLIError(f"No order details returned for {order_id}")
            return data[-1]
        if not isinstance(data, dict):
            raise AlgoCLIError("Order detail response is not an object")
        return data
