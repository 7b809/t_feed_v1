from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services import dummy_order_engine as engine_module
from app.services.dummy_order_engine import DummyOrderEngine
from app.services.dummy_order_store import DummyOrderStore


class FakeFeed:
    def latest(self, key):
        return {"received_at": "2026-11-12T09:41:01+05:30",
                "data": {"ltpc": {"ltp": 123.45}}}


class FakeTelegram:
    def __init__(self):
        self.messages = []

    async def send_message(self, title, message, level="INFO"):
        self.messages.append((title, message, level))
        return True


def build(tmp_path, **overrides):
    # Existing strategy tests replay a fixed historical trading session.
    # Explicitly simulate its clock and WebSocket provenance; strict freshness
    # defaults are covered in the dedicated gate tests below.
    fixed_now = datetime(2026, 11, 12, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    engine_module._now = lambda: fixed_now
    values = dict(
        live_signal_max_age_seconds=7200,
        live_quote_max_age_seconds=7200,
        live_frame_max_age_seconds=5,
        dummy_orders_enabled=True,
        dummy_supertrend_orders_enabled=True, dummy_order_quantity=1,
        dummy_order_state_dir=str(tmp_path), dummy_order_stop_loss_percent=30,
        dummy_target_upper_threshold=200, dummy_target_lower_threshold=100,
        dummy_target_points_above_upper=60, dummy_target_points_middle=50,
        dummy_target_points_below_lower=40,
    )
    values.update(overrides)
    config = SimpleNamespace(**values)
    store, telegram = DummyOrderStore(config), FakeTelegram()
    engine = DummyOrderEngine(config, store, FakeFeed(), telegram)
    original_handler = engine.on_supertrend_frame

    async def live_test_frame(payload, instrument_key, leg_role, isolation_id):
        await original_handler(
            payload, instrument_key, leg_role, isolation_id,
            event_origin="live_websocket",
            live_event_received_at=fixed_now.isoformat(),
        )

    engine.on_supertrend_frame = live_test_frame
    return engine, store


def isolation_payload():
    return {
        "trade_date_ist": "2026-11-12",
        "instrument_key": "CE",
        "trading_symbol": "NIFTY 25000 CE",
        "opposite_leg": {"instrument_key": "PE", "trading_symbol": "NIFTY 25000 PE"},
        "isolation_time": "2026-11-12T09:00:00+05:30",
    }


def test_target_point_bands(tmp_path):
    async def run():
        engine, _ = build(tmp_path)
        assert engine._target_points(250) == 60
        assert engine._target_points(150) == 50
        assert engine._target_points(100) == 50
        assert engine._target_points(99.99) == 40
    asyncio.run(run())


def test_supertrend_bullish_routes_same_leg(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        await engine.register_isolation("2026-11-12__CE", isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({"event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:18:00+05:30", "price": 110},
            "CE", "isolated", "2026-11-12__CE")
        assert store.load("2026-11-12__CE")["orders"]["supertrend"]["target_instrument_key"] == "CE"
    asyncio.run(run())


def test_pre_isolation_signal_is_ignored(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        payload = isolation_payload()
        payload["isolation_time"] = "2026-11-12T09:18:00+05:30"
        await engine.register_isolation("2026-11-12__CE", payload,
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({"event": "supertrend_signal", "signal": "BEARISH",
            "confirmed_time": "2026-11-12T09:39:00+05:30", "price": 110},
            "CE", "isolated", "2026-11-12__CE")
        assert store.load("2026-11-12__CE")["orders"]["supertrend"] is None
    asyncio.run(run())


def test_supertrend_bearish_routes_opposite(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        await engine.register_isolation("2026-11-12__CE", isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({"event": "supertrend_signal", "signal": "BEARISH",
            "confirmed_time": "2026-11-12T09:18:00+05:30", "price": 110},
            "CE", "isolated", "2026-11-12__CE")
        assert store.load("2026-11-12__CE")["orders"]["supertrend"]["target_instrument_key"] == "PE"
    asyncio.run(run())


def test_order_completes_at_target_and_tracks_high_low_and_duration(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:18:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        order = store.load(isolation_id)["orders"]["supertrend"]
        started = datetime.fromisoformat(order["started_at_ist"])
        tick1, tick2, tick3 = [(started + timedelta(seconds=s)).isoformat()
                              for s in (60, 90, 120)]
        await engine.on_market_tick("CE", {"ltpc": {"ltp": 140.0}}, tick1)
        await engine.on_market_tick("CE", {"ltpc": {"ltp": 110.0}}, tick2)
        await engine.on_market_tick("CE", {"ltpc": {"ltp": 173.45}}, tick3)
        order = store.load(isolation_id)["orders"]["supertrend"]
        assert order["status"] == "COMPLETED_TARGET"
        assert order["exit_reason"] == "TARGET" and order["exit_price"] == 173.45
        assert order["high_price"] == 173.45 and order["high_time_ist"] == tick3
        assert order["low_price"] == 110.0 and order["low_time_ist"] == tick2
        assert order["completed_at_ist"] == tick3 and order["duration_seconds"] == 120.0
        assert not engine._watch_index.get("CE")
    asyncio.run(run())


def test_order_completes_at_stop_loss(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:18:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        order = store.load(isolation_id)["orders"]["supertrend"]
        tick = (datetime.fromisoformat(order["started_at_ist"]) + timedelta(seconds=60)).isoformat()
        await engine.on_market_tick("CE", {"ltpc": {"ltp": 86.41}}, tick)
        order = store.load(isolation_id)["orders"]["supertrend"]
        assert order["status"] == "COMPLETED_STOP_LOSS"
        assert order["exit_reason"] == "STOP_LOSS" and order["exit_price"] == 86.41
        assert order["low_price"] == 86.41
    asyncio.run(run())



def test_supertrend_opposite_signal_does_not_reverse_open_order(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})

        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "RESISTANCE",
            "confirmed_time": "2026-11-12T09:18:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        first = store.load(isolation_id)["orders"]["supertrend"]
        assert first["target_instrument_key"] == "PE"
        assert first["status"] == "DUMMY_PLACED"

        # A later opposite signal cannot close or replace an open position.
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "SUPPORT",
            "confirmed_time": "2026-11-12T09:24:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)

        state = store.load(isolation_id)
        current = state["orders"]["supertrend"]
        assert current["order_id"] == first["order_id"]
        assert current["status"] == "DUMMY_PLACED"
        assert state.get("order_history", []) == []

    asyncio.run(run())


def test_supertrend_does_not_reverse_when_existing_target_is_already_met(tmp_path):
    async def run():
        class TargetFeed:
            def __init__(self):
                self.calls = 0

            def latest(self, key):
                self.calls += 1
                # First read supplies the entry; second read is the reversal
                # check against the already-open order's target.
                price = 123.45 if self.calls == 1 else 200.0
                return {"received_at": "2026-11-12T09:35:00+05:30",
                        "data": {"ltpc": {"ltp": price}}}

        config = SimpleNamespace(
            live_signal_max_age_seconds=7200,
            live_quote_max_age_seconds=7200,
            live_frame_max_age_seconds=5,
            dummy_orders_enabled=True,
            dummy_supertrend_orders_enabled=True, dummy_order_quantity=1,
            dummy_order_state_dir=str(tmp_path), dummy_order_stop_loss_percent=30,
            dummy_target_upper_threshold=200, dummy_target_lower_threshold=100,
            dummy_target_points_above_upper=60, dummy_target_points_middle=50,
            dummy_target_points_below_lower=40,
        )
        store = DummyOrderStore(config)
        engine = DummyOrderEngine(config, store, TargetFeed())
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "RESISTANCE",
            "confirmed_time": "2026-11-12T09:18:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        first = store.load(isolation_id)["orders"]["supertrend"]
        target_tick = (datetime.fromisoformat(first["started_at_ist"]) + timedelta(seconds=60)).isoformat()
        await engine.on_market_tick("PE", {"ltpc": {"ltp": 200.0}}, target_tick)
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "SUPPORT",
            "confirmed_time": "2026-11-12T09:24:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        state = store.load(isolation_id)
        assert state["orders"]["supertrend"]["status"] == "COMPLETED_TARGET"
        assert state["orders"]["supertrend"]["target_instrument_key"] == "PE"
        assert len(state.get("order_history", [])) == 1

    asyncio.run(run())



def test_startup_recovery_replays_two_entries_stop_then_target(tmp_path):
    async def run():
        class CandleHistory:
            def read_day(self, key, day):
                if key == "PE":
                    return [
                        {"ts": "2026-11-12T09:23:00+05:30", "open": 100, "high": 102, "low": 98, "close": 99},
                        {"ts": "2026-11-12T09:24:00+05:30", "open": 99, "high": 100, "low": 69, "close": 70},
                    ]
                return [
                    {"ts": "2026-11-12T09:44:00+05:30", "open": 100, "high": 102, "low": 98, "close": 101},
                    {"ts": "2026-11-12T09:24:00+05:30", "open": 101, "high": 151, "low": 100, "close": 150},
                ]

        engine, store = build(tmp_path)
        engine._candle_store = CandleHistory()
        isolation_id = "2026-11-12__CE"
        payload = isolation_payload()
        payload["isolation_time"] = "2026-11-12T09:00:00+05:30"
        await engine.register_isolation(isolation_id, payload,
                                        {"isolated": "CE", "opposite": "PE"})
        await engine._recover_supertrend_history(isolation_id, {
            "event": "snapshot", "signals": [
                {"signal": "BEARISH", "confirmed_time": "2026-11-12T09:22:00+05:30"},
                {"signal": "BULLISH", "confirmed_time": "2026-11-12T09:25:30+05:30"},
            ]
        })
        state = store.load(isolation_id)
        assert state["supertrend_startup_recovery"]["status"] == "COMPLETED"
        assert state["supertrend_order_count"] == 2
        assert len(state["order_history"]) == 2
        assert state["order_history"][0]["exit_reason"] == "STOP_LOSS"
        assert state["order_history"][1]["exit_reason"] == "TARGET"
        assert state["order_history"][1]["target_leg_role"] == "isolated"
        assert state["orders"]["supertrend"]["status"] == "COMPLETED_TARGET"

    asyncio.run(run())


def test_startup_recovery_waits_when_candle_history_is_missing(tmp_path):
    async def run():
        class EmptyHistory:
            def read_day(self, key, day):
                return []

        engine, store = build(tmp_path)
        engine._candle_store = EmptyHistory()
        isolation_id = "2026-11-12__CE"
        payload = isolation_payload()
        payload["isolation_time"] = "2026-11-12T09:00:00+05:30"
        await engine.register_isolation(isolation_id, payload,
                                        {"isolated": "CE", "opposite": "PE"})
        await engine._recover_supertrend_history(isolation_id, {
            "event": "snapshot", "signals": [
                {"signal": "BEARISH", "confirmed_time": "2026-11-12T09:22:00+05:30"}
            ]
        })
        state = store.load(isolation_id)
        assert state["supertrend_startup_recovery"]["status"] == "WAITING_FOR_HISTORY"
        assert state["orders"]["supertrend"] is None

    asyncio.run(run())


def test_supertrend_allows_only_two_entries_per_isolation(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})

        # Entry 1: bullish signal buys the isolated leg.
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:18:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        first = store.load(isolation_id)["orders"]["supertrend"]
        assert first["order_sequence"] == 1

        # A target closes entry 1; a later signal may still create entry 2.
        first_tick = (datetime.fromisoformat(first["started_at_ist"]) + timedelta(seconds=60)).isoformat()
        # A confirmed stop-loss (not a target win) unlocks the second entry.
        await engine.on_market_tick("CE", {"ltpc": {"ltp": 80.0}}, first_tick)
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BEARISH",
            "confirmed_time": "2026-11-12T09:24:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        second = store.load(isolation_id)["orders"]["supertrend"]
        assert second["order_sequence"] == 2
        assert second["target_instrument_key"] == "PE"
        assert store.load(isolation_id)["order_history"][0]["exit_reason"] == "STOP_LOSS"

        # Close entry 2. A third confirmed signal must not create another order.
        second_tick = (datetime.fromisoformat(second["started_at_ist"]) + timedelta(seconds=60)).isoformat()
        await engine.on_market_tick("PE", {"ltpc": {"ltp": 86.41}}, second_tick)

        # Recreate the engine to verify that persisted count survives restart.
        restarted_engine, restarted_store = build(tmp_path)
        await restarted_engine.register_isolation(isolation_id, isolation_payload(),
                                                  {"isolated": "CE", "opposite": "PE"})
        await restarted_engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:35:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)

        state = restarted_store.load(isolation_id)
        current = state["orders"]["supertrend"]
        assert current["order_id"] == second["order_id"]
        assert current["status"] == "COMPLETED_STOP_LOSS"
        assert state["supertrend_order_count"] == 2
        assert state["supertrend_order_limit"] == 2
        assert len(state["order_history"]) == 2
        assert [item["order_sequence"] for item in state["order_history"]] == [1, 2]

    asyncio.run(run())

def test_two_lot_target_takes_one_lot_and_moves_runner_stop(tmp_path):
    async def run():
        engine, store = build(tmp_path, dummy_order_quantity=2)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:18:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        order = store.load(isolation_id)["orders"]["supertrend"]
        assert order["quantity"] == 2
        assert order["quantity_units"] == 2
        assert order["status"] == "DUMMY_PLACED"

        stamp = (datetime.fromisoformat(order["started_at_ist"]) + timedelta(seconds=60)).isoformat()
        await engine.on_market_tick("CE", {"ltpc": {"ltp": 173.45}}, stamp)
        order = store.load(isolation_id)["orders"]["supertrend"]
        assert order["status"] == "DUMMY_PLACED"
        assert order["partial_exit_done"] is True
        assert order["first_target_reached"] is True
        assert order["quantity"] == 1
        assert order["quantity_units"] == 1
        assert order["partial_exit"]["exit_price"] == 173.45
        assert order["partial_exit"]["pnl_net"] == 50.0
        assert order["stop_loss_price"] == 148.45
        assert order["target_price"] == 173.45

        # A later touch of the runner stop closes the remaining lot and
        # combines its P&L with the already-realized first-lot profit.
        stamp2 = (datetime.fromisoformat(stamp) + timedelta(seconds=60)).isoformat()
        await engine.on_market_tick("CE", {"ltpc": {"ltp": 148.45}}, stamp2)
        order = store.load(isolation_id)["orders"]["supertrend"]
        assert order["status"] == "COMPLETED_STOP_LOSS"
        assert order["pnl_gross"] == 75.0
        assert order["pnl_net"] == 75.0

    asyncio.run(run())


def test_first_order_target_blocks_second_entry_even_inside_window(tmp_path):
    async def run():
        engine, store = build(tmp_path, dummy_order_quantity=1)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:18:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        first = store.load(isolation_id)["orders"]["supertrend"]
        stamp = (datetime.fromisoformat(first["started_at_ist"]) + timedelta(seconds=60)).isoformat()
        await engine.on_market_tick("CE", {"ltpc": {"ltp": 173.45}}, stamp)
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BEARISH",
            "confirmed_time": "2026-11-12T09:24:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        state = store.load(isolation_id)
        assert state["orders"]["supertrend"]["order_id"] == first["order_id"]
        assert state["supertrend_order_count"] == 1
        assert state["supertrend_second_entry_decision"]["status"] == "BLOCKED_FIRST_ORDER_PROFIT"
        assert state["supertrend_processed_signal_times"] == ["2026-11-12T09:24:00+05:30"]
        telegram_messages = list(engine._telegram.messages)
        assert any("SECOND ENTRY BLOCKED" in message[1] for message in telegram_messages)

        # Replayed duplicate signal is idempotent: no extra decision or Telegram alert.
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BEARISH",
            "confirmed_time": "2026-11-12T09:24:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        assert len(engine._telegram.messages) == len(telegram_messages)

    asyncio.run(run())


def test_late_first_signal_allows_only_one_entry_and_preserves_it(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T10:12:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        first = store.load(isolation_id)["orders"]["supertrend"]
        assert first["status"] == "DUMMY_PLACED"
        assert first["order_sequence"] == 1
        assert store.load(isolation_id)["supertrend_order_limit"] == 1

        # Opposite late signal is rejected before reversal; the first order
        # remains open and its target/stop-loss monitoring remains registered.
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BEARISH",
            "confirmed_time": "2026-11-12T10:20:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        state = store.load(isolation_id)
        current = state["orders"]["supertrend"]
        assert current["order_id"] == first["order_id"]
        assert current["status"] == "DUMMY_PLACED"
        assert current["target_instrument_key"] == "CE"
        assert state["supertrend_order_count"] == 1
        assert state.get("order_history", []) == []
        assert ("2026-11-12__CE", "supertrend") in engine._watch_index["CE"]
    asyncio.run(run())


def test_early_entry_window_rejects_signals_at_or_after_0940_without_reversal(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:20:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        first = store.load(isolation_id)["orders"]["supertrend"]
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BEARISH",
            "confirmed_time": "2026-11-12T09:40:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        state = store.load(isolation_id)
        assert state["orders"]["supertrend"]["order_id"] == first["order_id"]
        assert state["orders"]["supertrend"]["status"] == "DUMMY_PLACED"
        assert state["supertrend_order_count"] == 1
        assert state["supertrend_order_limit"] == 2
        assert state.get("order_history", []) == []
    asyncio.run(run())


def test_signal_before_0915_is_ignored(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        isolation_id = "2026-11-12__CE"
        payload = isolation_payload()
        payload["isolation_time"] = "2026-11-12T09:00:00+05:30"
        await engine.register_isolation(isolation_id, payload,
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:14:59+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        assert store.load(isolation_id)["orders"]["supertrend"] is None
    asyncio.run(run())



def test_live_execution_gate_rejects_stale_confirmation_and_quote(tmp_path):
    fixed_now = datetime(2026, 11, 19, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    engine_module._now = lambda: fixed_now

    class Feed:
        def __init__(self, received_at):
            self.received_at = received_at

        def latest(self, key):
            return {"received_at": self.received_at, "data": {"ltpc": {"ltp": 123.45}}}

    config = SimpleNamespace(
        dummy_order_stop_loss_percent=30, dummy_target_upper_threshold=200,
        dummy_target_lower_threshold=100, dummy_target_points_above_upper=60,
        dummy_target_points_middle=50, dummy_target_points_below_lower=40,
        live_signal_max_age_seconds=60, live_quote_max_age_seconds=10,
        live_frame_max_age_seconds=5,
    )
    engine = DummyOrderEngine(config, DummyOrderStore(config), Feed(fixed_now.isoformat()))
    state = {"trade_date_ist": fixed_now.date().isoformat()}
    payload = {"event": "supertrend_signal"}
    signal = {"dt": fixed_now - timedelta(seconds=15), "time": "fresh"}
    accepted = engine._live_entry_eligibility(
        payload, signal, "NSE_FO|TEST", state, "live_websocket", fixed_now.isoformat()
    )
    assert accepted["eligible"] is True
    assert accepted["stop_loss_price"] < accepted["entry"] < accepted["target_price"]

    stale_signal = dict(signal, dt=fixed_now - timedelta(seconds=61))
    rejected_signal = engine._live_entry_eligibility(
        payload, stale_signal, "NSE_FO|TEST", state, "live_websocket", fixed_now.isoformat()
    )
    assert rejected_signal["reason"] == "CONFIRMATION_TIME_STALE_OR_FUTURE"

    engine._live_feed = Feed((fixed_now - timedelta(seconds=11)).isoformat())
    rejected_quote = engine._live_entry_eligibility(
        payload, signal, "NSE_FO|TEST", state, "live_websocket", fixed_now.isoformat()
    )
    assert rejected_quote["reason"] == "LIVE_QUOTE_STALE"


def test_live_execution_gate_rejects_missing_websocket_provenance(tmp_path):
    fixed_now = datetime(2026, 11, 19, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    engine_module._now = lambda: fixed_now

    class Feed:
        def latest(self, key):
            return {"received_at": fixed_now.isoformat(), "data": {"ltpc": {"ltp": 123.45}}}

    config = SimpleNamespace(
        dummy_order_stop_loss_percent=30, dummy_target_upper_threshold=200,
        dummy_target_lower_threshold=100, dummy_target_points_above_upper=60,
        dummy_target_points_middle=50, dummy_target_points_below_lower=40,
        live_signal_max_age_seconds=60, live_quote_max_age_seconds=10,
        live_frame_max_age_seconds=5,
    )
    engine = DummyOrderEngine(config, DummyOrderStore(config), Feed())
    signal = {"dt": fixed_now - timedelta(seconds=5), "time": "fresh"}
    result = engine._live_entry_eligibility(
        {"event": "supertrend_signal"}, signal, "NSE_FO|TEST",
        {"trade_date_ist": fixed_now.date().isoformat()}, None, fixed_now.isoformat()
    )
    assert result["reason"] == "MISSING_LIVE_WEBSOCKET_PROVENANCE"


def test_restart_restores_mongo_open_order_and_replays_missed_candles(tmp_path):
    async def run():
        class MongoState:
            def load_isolation(self, date_key, isolation_id):
                return {
                    "isolation_id": isolation_id,
                    "isolated_instrument": {"instrument_key": "CE"},
                    "current_order": {
                        "order_id": "RECOVERY-SUPERTREND-2026-11-12__CE-091800",
                        "indicator": "SUPERTREND",
                        "status": "DUMMY_PLACED",
                        "target_instrument_key": "CE",
                        "target_leg_role": "isolated",
                        "instrument_key": "CE",
                        "entry_price": 100.0,
                        "stop_loss_price": 70.0,
                        "target_price": 150.0,
                        "quantity_units": 1,
                        "started_at_ist": "2026-11-12T09:20:00+05:30",
                        "entry_time_ist": "2026-11-12T09:20:00+05:30",
                        "price_source": "persisted_one_minute_ohlc",
                        "high_price": 100.0,
                        "low_price": 100.0,
                        "trigger": {"confirmed_time": "2026-11-12T09:18:00+05:30"},
                        "real_order_submitted": False,
                    },
                    "order_history": [],
                    "order_count": 1,
                }

        class CandleHistory:
            def read_day(self, key, day):
                if key == "CE":
                    return [
                        {"ts": "2026-11-12T09:20:00+05:30", "open": 100, "high": 105, "low": 95, "close": 102},
                        {"ts": "2026-11-12T09:21:00+05:30", "open": 102, "high": 110, "low": 69, "close": 75},
                    ]
                return []

        config = SimpleNamespace(
            dummy_orders_enabled=True, dummy_supertrend_orders_enabled=True,
            dummy_order_quantity=1, dummy_order_state_dir=str(tmp_path / "fresh"),
            dummy_order_stop_loss_percent=30, dummy_target_upper_threshold=200,
            dummy_target_lower_threshold=100, dummy_target_points_above_upper=60,
            dummy_target_points_middle=50, dummy_target_points_below_lower=40,
        )
        store = DummyOrderStore(config)
        engine = DummyOrderEngine(config, store, FakeFeed(), mongo_store=MongoState(),
                                  candle_store=CandleHistory())
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        restored = store.load(isolation_id)
        assert restored["orders"]["supertrend"]["order_id"].endswith("091800")
        assert restored["orders"]["supertrend"]["status"] == "DUMMY_PLACED"

        # A prior COMPLETED recovery marker must not block replay while an
        # existing DUMMY_PLACED order remains open.
        restored["supertrend_startup_recovery"] = {"status": "COMPLETED"}
        await asyncio.to_thread(store.save, isolation_id, restored)
        await engine.prepare_supertrend_recovery(
            isolation_id,
            datetime(2026, 11, 12, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
            datetime(2026, 11, 12, 9, 5, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        await engine._recover_supertrend_history(
            isolation_id, {"event": "snapshot", "signals": []},
            recovery_as_of=datetime(2026, 11, 12, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
            recovery_source="test_restart_replay",
        )
        final = store.load(isolation_id)
        closed = final["orders"]["supertrend"]
        assert closed["order_id"].endswith("091800")
        assert closed["status"] == "COMPLETED_STOP_LOSS"
        assert closed["exit_reason"] == "STOP_LOSS"
        assert closed["exit_price"] == 70.0
        assert final["supertrend_startup_recovery"]["status"] == "COMPLETED"
        assert final["supertrend_startup_recovery"]["orders_restored"] == 1
        assert final["supertrend_startup_recovery"]["orders_replayed"] == 1
        assert final["supertrend_startup_recovery"]["orders_completed"] == 1

    asyncio.run(run())


def test_pivot_support_adjusts_percentage_stop_to_nearest_support(tmp_path):
    engine, _ = build(tmp_path)

    class Pivots:
        def get(self, instrument_key, trade_date):
            return {"pivot_levels": {"S1": 60, "S2": 50, "S3": 40}}

    engine._pivot_service = Pivots()
    stop, details = engine._pivot_adjusted_stop_loss("CE", "2026-11-20", 120.20, 83.65)
    assert stop == 60.0
    assert details["selected_level"] == "S1"

    stop, details = engine._pivot_adjusted_stop_loss("CE", "2026-11-20", 120.20, 55.0)
    assert stop == 50.0
    assert details["selected_level"] == "S2"

    stop, details = engine._pivot_adjusted_stop_loss("CE", "2026-11-20", 120.20, 35.0)
    assert stop == 40.0
    assert details["selected_level"] == "S3_FLOOR"


def test_recovery_summary_counts_completed_current_order_without_order_history(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(
            isolation_id, isolation_payload(),
            {"isolated": "CE", "opposite": "PE"},
        )
        state = store.load(isolation_id)
        state["orders"]["supertrend"] = {
            "order_id": "RECOVERY-SUPERTREND-2026-11-12__CE-091800",
            "indicator": "SUPERTREND",
            "simulation_mode": "STARTUP_RECOVERY",
            "price_source": "persisted_one_minute_ohlc",
            "status": "COMPLETED_STOP_LOSS",
            "exit_reason": "STOP_LOSS",
            "started_at_ist": "2026-11-12T09:19:00+05:30",
            "completed_at_ist": "2026-11-12T09:30:00+05:30",
        }
        state["order_history"] = []
        store.save(isolation_id, state)
        engine._candle_store = type("History", (), {
            "read_day": lambda self, key, day: []
        })()

        await engine._recover_supertrend_history(
            isolation_id, {"event": "snapshot", "signals": []},
            recovery_as_of=datetime(2026, 11, 12, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        final = store.load(isolation_id)
        summary = final["supertrend_startup_recovery"]
        assert final["orders"]["supertrend"]["status"] == "COMPLETED_STOP_LOSS"
        assert summary["orders_restored"] == 1
        assert summary["orders_replayed"] == 1
        assert summary["orders_completed"] == 1
        assert summary["orders_replayed_this_run"] == 0
        assert summary["orders_completed_this_run"] == 0

    asyncio.run(run())


def test_open_order_recovery_is_caught_up_not_marked_completed(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        state = store.load(isolation_id)
        state["orders"]["supertrend"] = {
            "order_id": "existing-open", "indicator": "SUPERTREND",
            "status": "DUMMY_PLACED", "target_instrument_key": "CE",
            "target_leg_role": "isolated", "entry_price": 100.0,
            "stop_loss_price": 70.0, "target_price": 150.0,
            "quantity_units": 1, "started_at_ist": "2026-11-12T09:20:00+05:30",
            "price_source": "persisted_one_minute_ohlc",
            "high_price": 100.0, "low_price": 100.0,
        }
        store.save(isolation_id, state)
        engine._candle_store = type("History", (), {
            "read_day": lambda self, key, day: [
                {"ts": "2026-11-12T09:20:00+05:30", "open": 100, "high": 105, "low": 95, "close": 102}
            ]
        })()
        await engine._recover_supertrend_history(
            isolation_id, {"event": "snapshot", "signals": []},
            recovery_as_of=datetime(2026, 11, 12, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        final = store.load(isolation_id)
        assert final["orders"]["supertrend"]["status"] == "DUMMY_PLACED"
        assert final["supertrend_startup_recovery"]["status"] == "CAUGHT_UP"

    asyncio.run(run())
