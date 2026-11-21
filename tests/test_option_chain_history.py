from __future__ import annotations

import json

from services.option_chain_history import seed_option_chain_history


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_seed_merges_history_in_timestamp_order_and_only_once(tmp_path, monkeypatch):
    option_root = tmp_path / "option_chain"
    isolated_root = tmp_path / "isolated"
    monkeypatch.setenv("OPTION_CHAIN_DATA_DIR", str(option_root))
    monkeypatch.setenv("ISOLATED_DATA_DIR", str(isolated_root))
    contract = option_root / "nifty" / "20261120" / "25100_ce_NSE_FO_64473"
    (contract / "meta.json").parent.mkdir(parents=True)
    (contract / "meta.json").write_text(json.dumps({"instrument_key": "NSE_FO|64473"}))
    _write_jsonl(contract / "live_feed.jsonl", [
        {"received_at_ist": "2026-11-20T09:15:00+05:30", "value": "before"},
        {"received_at_ist": "2026-11-20T09:16:00+05:30", "value": "during"},
    ])
    destination = isolated_root / "live_feed" / "isolated_NSE_FO_64473.jsonl"
    _write_jsonl(destination, [{"received_at_ist": "2026-11-20T09:15:30+05:30", "value": "isolated_live"}])

    result = seed_option_chain_history("NSE_FO|64473", "2026-11-20", "isolated", "live_feed", destination)
    assert result["state"] == "seeded"
    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert [row["value"] for row in rows] == ["before", "isolated_live", "during"]
    again = seed_option_chain_history("NSE_FO|64473", "2026-11-20", "isolated", "live_feed", destination)
    assert again["state"] == "already_seeded"
    assert len(destination.read_text().splitlines()) == 3


def test_seed_retries_if_source_file_appears_later(tmp_path, monkeypatch):
    option_root = tmp_path / "option_chain"
    isolated_root = tmp_path / "isolated"
    monkeypatch.setenv("OPTION_CHAIN_DATA_DIR", str(option_root))
    monkeypatch.setenv("ISOLATED_DATA_DIR", str(isolated_root))
    contract = option_root / "nifty" / "20261120" / "25100_ce_NSE_FO_64473"
    contract.mkdir(parents=True)
    (contract / "meta.json").write_text(json.dumps({"instrument_key": "NSE_FO|64473"}))
    destination = isolated_root / "supertrend" / "2026-11-20_isolated_NSE_FO_64473.jsonl"
    result = seed_option_chain_history("NSE_FO|64473", "2026-11-20", "isolated", "supertrend", destination)
    assert result["state"] == "source_missing"
    _write_jsonl(contract / "supertrend_feed.jsonl", [
        {"received_at_ist": "2026-11-20T09:15:00+05:30", "event": "snapshot"},
    ])
    result = seed_option_chain_history("NSE_FO|64473", "2026-11-20", "isolated", "supertrend", destination)
    assert result["state"] == "seeded"
    assert len(destination.read_text().splitlines()) == 1
