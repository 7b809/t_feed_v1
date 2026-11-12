from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.live_feed_state_store import LiveFeedStateStore
from app.services.live_feed_ws_client_service import LiveFeedWsClientService

IST = ZoneInfo("Asia/Kolkata")


def _client() -> LiveFeedWsClientService:
    config = SimpleNamespace(
        live_feed_enabled=True,
        live_feed_ws_url="",
        live_feed_port=8004,
        live_feed_mode="full",
        live_feed_state_dir="./data/live_feed",
        live_feed_start_hour=9,
        live_feed_start_minute=0,
        live_feed_end_hour=16,
        live_feed_end_minute=0,
        live_feed_reconnect_max_backoff_seconds=30,
        nifty_instrument_key="NSE_INDEX|Nifty 50",
    )
    return LiveFeedWsClientService(config, state_store=None)


def test_live_feed_window_uses_ist_inclusive_start_exclusive_end() -> None:
    client = _client()
    assert not client._inside_window(datetime(2026, 11, 12, 8, 59, 59, tzinfo=IST))
    assert client._inside_window(datetime(2026, 11, 12, 9, 0, 0, tzinfo=IST))
    assert client._inside_window(datetime(2026, 11, 12, 15, 59, 59, tzinfo=IST))
    assert not client._inside_window(datetime(2026, 11, 12, 16, 0, 0, tzinfo=IST))


def test_live_feed_next_boundary_calculation() -> None:
    client = _client()
    before_open = datetime(2026, 11, 12, 8, 59, 0, tzinfo=IST)
    before_close = datetime(2026, 11, 12, 15, 59, 0, tzinfo=IST)
    after_close = datetime(2026, 11, 12, 16, 0, 0, tzinfo=IST)
    assert client._seconds_until_boundary(before_open) == 60
    assert client._seconds_until_boundary(before_close) == 60
    assert client._seconds_until_boundary(after_close) == 17 * 60 * 60


def test_shared_context_deduplicates_nifty_for_multiple_isolations() -> None:
    client = _client()
    client._isolation_legs = {
        "iso-a": {"isolated": "NSE_FO|101", "opposite": "NSE_FO|102"},
        "iso-b": {"isolated": "NSE_FO|201", "opposite": "NSE_FO|202"},
    }
    context = client._desired_context()
    assert set(context) == {
        "NSE_FO|101", "NSE_FO|102", "NSE_FO|201", "NSE_FO|202",
        "NSE_INDEX|Nifty 50",
    }
    assert context["NSE_INDEX|Nifty 50"]["roles"] == {"underlying"}
    assert context["NSE_INDEX|Nifty 50"]["isolation_ids"] == {"iso-a", "iso-b"}


def test_live_feed_store_preserves_raw_payload_and_daily_history(tmp_path: Path) -> None:
    store = LiveFeedStateStore(SimpleNamespace(live_feed_state_dir=str(tmp_path)))
    raw = {
        "fullFeed": {
            "marketFF": {
                "ltpc": {"ltp": 245.5, "ltt": "1794468000000"},
                "marketLevel": {"bidAskQuote": [{"bidP": 245.4, "askP": 245.6}]},
            }
        }
    }
    store.save_message(
        "NSE_FO|101", raw, "full", ["iso-a", "iso-b"], ["isolated", "opposite"]
    )
    latest = store.latest("NSE_FO|101")
    assert latest is not None
    assert latest["data"] == raw
    assert latest["isolation_ids"] == ["iso-a", "iso-b"]
    assert latest["roles"] == ["isolated", "opposite"]

    instrument_dir = tmp_path / "NSE_FO_101"
    daily_files = list(instrument_dir.glob("*.jsonl"))
    assert len(daily_files) == 1
    saved = json.loads(daily_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert saved["data"] == raw
