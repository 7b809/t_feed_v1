import json

from services import gtt_execution_audit_service as audit_module
from services.gtt_execution_audit_service import GTTExecutionAuditService


class _FakeCollection:
    def update_one(self, *args, **kwargs):
        return None


class _FakeDatabase:
    def __getitem__(self, key):
        return _FakeCollection()


def test_audit_keeps_predefined_prompt_separate_from_deterministic_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_module, "get_mongo_db", lambda: _FakeDatabase())
    service = GTTExecutionAuditService()
    service.root = tmp_path

    rendered = "Use the upstox skill to fetch LTP for NSE_FO|12345."
    contract = '{"task":"Record execution audit"}'
    event = service.append(
        "EXECUTION_BLOCKED",
        "2026-11-25__NSE_INDEX|Nifty 50",
        rendered,
        {"instrument_key": "NSE_FO|12345"},
        {"cli_error": "FileNotFoundError"},
        "BLOCKED",
        metadata={"algocli": {
            "template": "get_ltp.txt",
            "executable": "/home/ubuntu/.local/bin/algoai",
            "stage": "GET_LTP",
            "process_started": False,
        }},
        prompt_origin="algocli_template",
        execution_stage="GET_LTP",
        audit_contract=contract,
    )

    assert event["prompt_origin"] == "algocli_template"
    assert event["prompt"] == rendered
    assert event["audit_contract"] == contract
    assert event["execution_stage"] == "GET_LTP"

    saved = json.loads((tmp_path / f"{event['trade_date_ist']}.json").read_text())
    saved_event = saved["events"][-1]
    assert saved_event["prompt"] == rendered
    assert saved_event["audit_contract"] == contract
    assert saved_event["metadata"]["algocli"]["template"] == "get_ltp.txt"
