"""Direct CLI adapter for the installed AlgoCLI command.

The isolation application invokes only:
    algoai --skip-permissions --prompt "<rendered prompt>"
No AlgoCLI project directory, launcher script, Python interpreter, or broker
credentials are configured or accessed by this application.
"""
from __future__ import annotations

import json
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path
from string import Template
from typing import Any

from core.logger import get_logger

logger = get_logger(__file__)
CLI_TIMEOUT_SECONDS = 180


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
    """Run predefined prompt templates using the installed algoai command."""

    def __init__(self) -> None:
        self.last_execution: dict[str, Any] = {}

    def _template(self, name: str, values: dict[str, Any]) -> str:
        root = Path(__file__).resolve().parents[1] / "prompts" / "algocli"
        path = root / name
        if not path.is_file():
            raise AlgoCLIError(f"Prompt template is missing: {name}")
        template = Template(path.read_text(encoding="utf-8"))
        try:
            return template.substitute({k: str(v) for k, v in values.items()})
        except KeyError as exc:
            raise AlgoCLIError(f"Missing prompt value for {exc.args[0]} in {name}") from exc

    @staticmethod
    def _json_from_stdout(stdout: str) -> Any:
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
            raise AlgoCLIError("algoai did not return valid JSON on stdout")
        return max(candidates, key=lambda item: item[0])[1]

    @staticmethod
    def _unwrap(value: Any) -> Any:
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
        operation_id = str(values.get("operation_id") or uuid.uuid4().hex)
        prompt = self._template(template_name, {**values, "operation_id": operation_id})

        # Invoke only the installed CLI. No project path, script, interpreter,
        # custom working directory, or broker credential is passed.
        command = ["algoai", "--skip-permissions", "--prompt", prompt]
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise AlgoCLIError(
                "The algoai command is not installed or is not available on PATH."
            ) from exc

        try:
            stdout, stderr = proc.communicate(timeout=CLI_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            try:
                import os
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, AttributeError):
                proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    import os
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, AttributeError):
                    proc.kill()
                proc.communicate()
            self.last_execution = {
                "operation_id": operation_id,
                "template": template_name,
                "prompt": prompt,
                "exit_code": None,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stderr": _redact_text(str(exc)),
                "outcome": "AMBIGUOUS" if write else "FAILED",
                "command": "algoai --skip-permissions --prompt <template>",
            }
            if write:
                raise AlgoCLIWriteAmbiguous(
                    f"algoai timed out after {CLI_TIMEOUT_SECONDS}s; broker outcome is unknown"
                ) from exc
            raise AlgoCLIError(f"algoai timed out after {CLI_TIMEOUT_SECONDS}s") from exc

        duration = round(time.monotonic() - started, 3)
        self.last_execution = {
            "operation_id": operation_id,
            "template": template_name,
            "prompt": prompt,
            "exit_code": proc.returncode,
            "duration_seconds": duration,
            "stderr": _redact_text(stderr[-4000:]),
            "outcome": "COMPLETED" if proc.returncode == 0 else ("AMBIGUOUS" if write else "FAILED"),
            "command": "algoai --skip-permissions --prompt <template>",
        }
        if proc.returncode != 0:
            message = _redact_text(
                f"algoai exited with code {proc.returncode}: {stderr[-1500:] or stdout[-1500:]}"
            )
            if write:
                raise AlgoCLIWriteAmbiguous(
                    message + " (write outcome is unknown; reconcile before any retry)"
                )
            raise AlgoCLIError(message)

        try:
            parsed = self._json_from_stdout(stdout)
            payload = self._unwrap(parsed)
            if not isinstance(payload, (dict, list)):
                raise AlgoCLIError("algoai JSON response must be an object or array")
        except AlgoCLIError as exc:
            if write:
                self.last_execution["outcome"] = "AMBIGUOUS"
                raise AlgoCLIWriteAmbiguous(
                    f"{exc}; write outcome is unknown. Reconcile before retrying."
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
                "command": "algoai --skip-permissions --prompt <template>",
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
        return {"last_price": float(price), "provider_timestamp": data.get("timestamp"), "raw": result}

    def place_gtt_bracket(self, instrument_key, lots, stop, target, operation_id=None):
        lots = int(lots)
        if lots <= 0:
            raise AlgoCLIError("GTT lot count must be a positive integer")
        return self._run("place_gtt.txt", {
            "instrument_key": instrument_key,
            "lots": lots,
            "lot_label": f"{lots} lot" if lots == 1 else f"{lots} lots",
            "sl_price": stop,
            "target_price": target,
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
                raise AlgoCLIError("Could not verify exactly one STOPLOSS rule after algoai modification")
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

    def place_market_exit(self, instrument_key, lots):
        lots = int(lots)
        if lots <= 0:
            raise AlgoCLIError("Market-exit lot count must be a positive integer")
        return self._run("place_market_exit.txt", {
            "instrument_key": instrument_key,
            "lots": lots,
            "lot_label": f"{lots} lot" if lots == 1 else f"{lots} lots",
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
