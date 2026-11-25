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
    result, client = _call(tmp_path, monkeypatch, api_token="DO_NOT_SAVE")
    assert result["status"] == "DRY_RUN"
    assert result["executed"] is False
    assert client.last_execution == {}
    folder = tmp_path / result["artifact_directory"].split("/")[-1]
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    response = json.loads((folder / "response.json").read_text(encoding="utf-8"))
    prompt = (folder / "prompt.txt").read_text(encoding="utf-8")
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert request["instrument_key"] == "NSE_FO|64473"
    assert request["trading_symbol"] == "NIFTY 25500 CE 23 NOV 26"
    assert request["target"] == 170
    assert request["stoploss"] == 84
    assert "api_token" not in request
    assert "DO_NOT_SAVE" not in prompt
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
