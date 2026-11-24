import json
import subprocess

import pytest

from clients.algocli_client import AlgoCLIClient, AlgoCLIError, AlgoCLIWriteAmbiguous


def test_json_parser_accepts_cli_status_lines():
    raw = '[status] running\n{"data":{"gtt_order_id":"GTT-123"}}\n[status] done'
    assert AlgoCLIClient._json_from_stdout(raw) == {"data": {"gtt_order_id": "GTT-123"}}


def test_json_parser_rejects_missing_json():
    with pytest.raises(AlgoCLIError):
        AlgoCLIClient._json_from_stdout("algoai failed without structured output")


def test_unwraps_json_result_envelope():
    raw = {"result": json.dumps({"data": {"status": "complete"}})}
    assert AlgoCLIClient._unwrap(raw) == {"data": {"status": "complete"}}


def test_predefined_prompt_substitution():
    client = AlgoCLIClient()
    prompt = client._template("place_gtt.txt", {
        "instrument_key": "NSE_FO|12345", "lots": 1, "lot_label": "1 lot",
        "sl_price": 70, "target_price": 140,
    })
    assert "NSE_FO|12345" in prompt
    assert "1 lot" in prompt
    assert "stop-loss 70" in prompt
    assert "target 140" in prompt
    assert "entry-price" not in prompt
    assert "product" not in prompt
    assert "$instrument_key" not in prompt


def test_missing_template_value_fails_closed():
    with pytest.raises(AlgoCLIError):
        AlgoCLIClient()._template("place_gtt.txt", {"instrument_key": "NSE_FO|12345"})


def test_invokes_only_algoai_cli_with_skip_permissions(monkeypatch):
    client = AlgoCLIClient()
    monkeypatch.setattr(client, "_template", lambda name, values: "Use skill. Return JSON.")
    captured = {}

    class FakeProcess:
        pid = 123
        returncode = 0
        def communicate(self, timeout=None):
            return '{"data":{"last_price":123.4}}', ""

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    result = client._run("get_ltp.txt", {})
    assert captured["command"] == [
        "algoai", "--skip-permissions", "--prompt", "Use skill. Return JSON."
    ]
    assert "cwd" not in captured["kwargs"]
    result_data = result["response"]
    assert result_data == {"data": {"last_price": 123.4}}


def test_missing_algoai_command_fails_closed(monkeypatch):
    client = AlgoCLIClient()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(AlgoCLIError, match="algoai"):
        client._run("get_ltp.txt", {})


def test_nonzero_write_exit_is_ambiguous(monkeypatch):
    client = AlgoCLIClient()
    monkeypatch.setattr(client, "_template", lambda name, values: "place order")
    class FakeProcess:
        pid = 123
        returncode = 1
        def communicate(self, timeout=None):
            return "", "CLI exited after request"
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProcess())
    with pytest.raises(AlgoCLIWriteAmbiguous):
        client._run("place_gtt.txt", {}, write=True)
    assert client.last_execution["outcome"] == "AMBIGUOUS"


def test_modify_gtt_stop_requires_verified_broker_rule(monkeypatch):
    client = AlgoCLIClient()
    monkeypatch.setattr(client, "_run", lambda *args, **kwargs: {
        "response": {"status": "accepted"},
        "_algocli": {"operation_id": "modify-1", "prompt": "rendered prompt"},
    })
    monkeypatch.setattr(client, "get_gtt", lambda gtt_id: {
        "gtt_order_id": gtt_id,
        "rules": [{"strategy": "STOPLOSS", "trigger_price": 82.5}],
    })
    result = client.modify_gtt_stop("GTT-1", 82.5)
    assert result["verified_gtt"]["rules"][0]["trigger_price"] == 82.5
    assert result["_algocli"]["operation_id"] == "modify-1"


def test_modify_gtt_stop_rejects_unverified_price(monkeypatch):
    client = AlgoCLIClient()
    monkeypatch.setattr(client, "_run", lambda *args, **kwargs: {
        "response": {"status": "preview_only"},
        "_algocli": {"operation_id": "modify-2", "prompt": "rendered prompt"},
    })
    monkeypatch.setattr(client, "get_gtt", lambda gtt_id: {
        "gtt_order_id": gtt_id,
        "rules": [{"strategy": "STOPLOSS", "trigger_price": 80.0}],
    })
    with pytest.raises(AlgoCLIWriteAmbiguous):
        client.modify_gtt_stop("GTT-1", 82.5)
    assert client.last_execution["outcome"] == "AMBIGUOUS"
