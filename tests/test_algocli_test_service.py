from __future__ import annotations

import json

import pytest

from services import algocli_test_service as service_module
from services.algocli_test_service import AlgoCLITestService


class FakeClient:
    def __init__(self):
        from clients.algocli_client import AlgoCLIClient
        self.template_client = AlgoCLIClient()
        self.last_execution = {}

    def _template(self, name, values):
        return self.template_client._template(name, values)

    def _run(self, name, values, write=False):
        assert name == "test_place_gtt.txt"
        assert write is True
        self.last_execution = {
            "operation_id": values["operation_id"],
            "template": name,
            "prompt": self._template(name, values),
            "exit_code": 0,
            "outcome": "COMPLETED",
        }
        return {"response": {"gtt_order_id": "GTT-TEST-123", "status": "success"}}


def _call(tmp_path, monkeypatch, **overrides):
    monkeypatch.setattr(service_module, "ALGOCLI_TEST_DATA_DIR", str(tmp_path))
    client = FakeClient()
    payload = {
        "instrument_key": "NSE_FO|64473",
        "trading_symbol": "NIFTY 25500 CE 23 NOV 26",
        "entry": None,
        "target": 170,
        "stoploss": 84,
        "lots": 1,
        "confirm_live_order": False,
        "live_writes_enabled": False,
        "operation_id": "test-order-001",
    }
    payload.update(overrides)
    result = AlgoCLITestService(client).place_gtt(**payload)
    return result, client


def test_dry_run_generates_prompt_and_saves_each_artifact(tmp_path, monkeypatch):
    result, client = _call(tmp_path, monkeypatch)
    assert result["status"] == "DRY_RUN"
    assert result["executed"] is False
    assert client.last_execution == {}
    folder = tmp_path / "place_gtt" / result["artifact_directory"].split("/")[-1]
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    response = json.loads((folder / "response.json").read_text(encoding="utf-8"))
    prompt = (folder / "prompt.txt").read_text(encoding="utf-8")
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert request["instrument_key"] == "NSE_FO|64473"
    assert request["trading_symbol"] == "NIFTY 25500 CE 23 NOV 26"
    assert request["target"] == 170
    assert request["stoploss"] == 84
    assert "api_token" not in request
    assert "NIFTY 25500 CE 23 NOV 26" in prompt
    assert "stop-loss to 84.0" in prompt
    assert "target to 170.0" in prompt
    assert response["status"] == "DRY_RUN"
    assert metadata["status"] == "DRY_RUN"


def test_live_write_requires_server_side_gate(tmp_path, monkeypatch):
    result, client = _call(
        tmp_path, monkeypatch,
        confirm_live_order=True,
        live_writes_enabled=False,
    )
    assert result["status"] == "BLOCKED"
    assert result["executed"] is False
    assert client.last_execution == {}


def test_confirmed_live_write_calls_algocli_and_archives_response(tmp_path, monkeypatch):
    result, client = _call(
        tmp_path, monkeypatch,
        confirm_live_order=True,
        live_writes_enabled=True,
    )
    assert result["status"] == "COMPLETED"
    assert result["response"]["response"]["gtt_order_id"] == "GTT-TEST-123"
    folder = tmp_path / result["artifact_directory"].split("/")[-1]
    saved = json.loads((folder / "response.json").read_text(encoding="utf-8"))
    assert saved["response"]["gtt_order_id"] == "GTT-TEST-123"
    assert client.last_execution["template"] == "test_place_gtt.txt"


def test_rejects_unsupported_entry_price(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "ALGOCLI_TEST_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="entry=null"):
        AlgoCLITestService(FakeClient()).place_gtt(
            instrument_key="NSE_FO|64473",
            trading_symbol="NIFTY 25500 CE 23 NOV 26",
            entry=100,
            target=170,
            stoploss=84,
        )


def test_rejects_invalid_stop_and_target(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "ALGOCLI_TEST_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="stoploss"):
        AlgoCLITestService(FakeClient()).place_gtt(
            instrument_key="NSE_FO|64473",
            trading_symbol="NIFTY 25500 CE 23 NOV 26",
            entry=None,
            target=80,
            stoploss=84,
        )


def test_redactor_removes_credential_fields_and_inline_tokens():
    from services.algocli_test_service import _redact
    cleaned = _redact({
        "api_token": "sensitive",
        "message": "Bearer secret-token",
        "result": {"access_token": "hidden", "status": "ok"},
    })
    assert "api_token" not in cleaned
    assert cleaned["message"] == "Bearer [REDACTED]"
    assert "access_token" not in cleaned["result"]



class FakeHealthClient:
    def __init__(self, *, fail=False):
        self.last_execution = {}
        self.fail = fail

    def _template(self, name, values):
        assert name == "get_positions.txt"
        assert values == {}
        return "Use the upstox-pnl skill to fetch current positions. Return JSON only."

    def _run(self, name, values, write=False):
        assert name == "get_positions.txt"
        assert write is False
        self.last_execution = {
            "template": name,
            "stage": "GET_POSITIONS",
            "outcome": "COMPLETED",
            "duration_seconds": 0.12,
            "process_started": True,
        }
        if self.fail:
            self.last_execution["outcome"] = "LAUNCH_FAILED"
            raise RuntimeError("algoai executable unavailable")
        return {
            "response": {"positions": [{"instrument_key": "NSE_FO|123", "quantity": 15}]},
            "_algocli": {"template": name, "stage": "GET_POSITIONS"},
        }


def test_get_positions_health_is_queued_without_running_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "ALGOCLI_TEST_DATA_DIR", str(tmp_path))
    client = FakeHealthClient()
    service = AlgoCLITestService(client)
    job = service.prepare_get_positions_health()
    assert job["artifact_directory"].startswith(str(tmp_path / "get_positions"))
    assert job["result_path"].endswith("/response.json")
    assert client.last_execution == {}
    queued = service.read_get_positions_health(job["operation_id"])
    assert queued["status"] == "ACCEPTED"
    folder = tmp_path / "get_positions" / job["artifact_directory"].split("/")[-1]
    assert (folder / "prompt.txt").read_text(encoding="utf-8").startswith("Use the upstox-pnl")


def test_get_positions_health_background_worker_saves_final_response(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "ALGOCLI_TEST_DATA_DIR", str(tmp_path))
    client = FakeHealthClient()
    service = AlgoCLITestService(client)
    job = service.prepare_get_positions_health()
    service.run_get_positions_health(job["operation_id"], job["artifact_directory"])
    result = service.read_get_positions_health(job["operation_id"])
    assert result["status"] == "HEALTHY"
    assert result["healthy"] is True
    assert result["response"]["response"]["positions"][0]["quantity"] == 15
    folder = tmp_path / "get_positions" / job["artifact_directory"].split("/")[-1]
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["cli_execution"]["stage"] == "GET_POSITIONS"


def test_get_positions_health_background_worker_archives_cli_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "ALGOCLI_TEST_DATA_DIR", str(tmp_path))
    client = FakeHealthClient(fail=True)
    service = AlgoCLITestService(client)
    job = service.prepare_get_positions_health()
    service.run_get_positions_health(job["operation_id"], job["artifact_directory"])
    result = service.read_get_positions_health(job["operation_id"])
    assert result["status"] == "UNHEALTHY"
    assert result["healthy"] is False
    assert "unavailable" in result["response"]["error"]
    with pytest.raises(ValueError, match="Invalid operation_id"):
        service.read_get_positions_health("../escape")
