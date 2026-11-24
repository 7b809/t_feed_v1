import json
import subprocess

import pytest

from clients.algocli_client import AlgoCLIClient, AlgoCLIError, AlgoCLIWriteAmbiguous


def test_json_parser_accepts_launcher_timing_lines():
    raw = '[TIMING] Start: now\n{"data":{"gtt_order_id":"GTT-123"}}\n[TIMING] Duration: 1s'
    assert AlgoCLIClient._json_from_stdout(raw) == {"data": {"gtt_order_id": "GTT-123"}}


def test_json_parser_rejects_missing_json():
    with pytest.raises(AlgoCLIError):
        AlgoCLIClient._json_from_stdout("launcher failed without structured output")


def test_unwraps_json_result_envelope():
    raw = {"result": json.dumps({"data": {"status": "complete"}})}
    assert AlgoCLIClient._unwrap(raw) == {"data": {"status": "complete"}}


def test_predefined_prompt_substitution():
    client = AlgoCLIClient()
    prompt = client._template(
        "place_gtt.txt",
        {
            "instrument_key": "NSE_FO|12345",
            "quantity": 25,
            "product": "I",
            "entry_price": 100,
            "sl_price": 70,
            "target_price": 140,
        },
    )
    assert "NSE_FO|12345" in prompt
    assert "quantity 25" in prompt
    assert "$instrument_key" not in prompt


def test_missing_template_value_fails_closed():
    client = AlgoCLIClient()
    with pytest.raises(AlgoCLIError):
        client._template("place_gtt.txt", {"instrument_key": "NSE_FO|12345"})


def test_nonzero_write_exit_is_ambiguous(tmp_path, monkeypatch):
    launcher = tmp_path / "launch_upstox_agy.py"
    launcher.write_text("# launcher placeholder", encoding="utf-8")
    client = AlgoCLIClient()
    client.project_dir = tmp_path
    client.launcher = launcher

    class FakeProcess:
        returncode = 1

        def communicate(self, timeout=None):
            return "", "CLI exited after request"

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    with pytest.raises(AlgoCLIWriteAmbiguous):
        client._run(
            "place_gtt.txt",
            {
                "instrument_key": "NSE_FO|12345",
                "quantity": 25,
                "product": "I",
                "entry_price": 100,
                "sl_price": 70,
                "target_price": 140,
            },
            write=True,
        )
    assert client.last_execution["outcome"] == "AMBIGUOUS"



def test_modify_gtt_stop_requires_verified_broker_rule(monkeypatch):
    client = AlgoCLIClient()
    monkeypatch.setattr(
        client,
        "_run",
        lambda *args, **kwargs: {
            "response": {"status": "accepted"},
            "_algocli": {"operation_id": "modify-1", "prompt": "rendered prompt"},
        },
    )
    monkeypatch.setattr(
        client,
        "get_gtt",
        lambda gtt_id: {
            "gtt_order_id": gtt_id,
            "rules": [{"strategy": "STOPLOSS", "trigger_price": 82.5}],
        },
    )
    result = client.modify_gtt_stop("GTT-1", 82.5)
    assert result["verified_gtt"]["rules"][0]["trigger_price"] == 82.5
    assert result["_algocli"]["operation_id"] == "modify-1"


def test_modify_gtt_stop_rejects_unverified_price(monkeypatch):
    client = AlgoCLIClient()
    monkeypatch.setattr(
        client,
        "_run",
        lambda *args, **kwargs: {
            "response": {"status": "preview_only"},
            "_algocli": {"operation_id": "modify-2", "prompt": "rendered prompt"},
        },
    )
    monkeypatch.setattr(
        client,
        "get_gtt",
        lambda gtt_id: {
            "gtt_order_id": gtt_id,
            "rules": [{"strategy": "STOPLOSS", "trigger_price": 80.0}],
        },
    )
    with pytest.raises(AlgoCLIWriteAmbiguous):
        client.modify_gtt_stop("GTT-1", 82.5)
    assert client.last_execution["outcome"] == "AMBIGUOUS"
