from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

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
    values = dict(
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
    return DummyOrderEngine(config, store, FakeFeed(), telegram), store


def isolation_payload():
    return {
        "trade_date_ist": "2026-11-12",
        "instrument_key": "CE",
        "trading_symbol": "NIFTY 25000 CE",
        "opposite_leg": {"instrument_key": "PE", "trading_symbol": "NIFTY 25000 PE"},
        "isolation_time": "2026-11-12T09:30:00+05:30",
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
            "confirmed_time": "2026-11-12T09:40:00+05:30", "price": 110},
            "CE", "isolated", "2026-11-12__CE")
        assert store.load("2026-11-12__CE")["orders"]["supertrend"]["target_instrument_key"] == "CE"
    asyncio.run(run())


def test_pre_isolation_signal_is_ignored(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        payload = isolation_payload()
        payload["isolation_time"] = "2026-11-12T09:40:00+05:30"
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
            "confirmed_time": "2026-11-12T09:40:00+05:30", "price": 110},
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
            "confirmed_time": "2026-11-12T09:40:00+05:30", "price": 123.45,
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
            "confirmed_time": "2026-11-12T09:40:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        order = store.load(isolation_id)["orders"]["supertrend"]
        tick = (datetime.fromisoformat(order["started_at_ist"]) + timedelta(seconds=60)).isoformat()
        await engine.on_market_tick("CE", {"ltpc": {"ltp": 86.41}}, tick)
        order = store.load(isolation_id)["orders"]["supertrend"]
        assert order["status"] == "COMPLETED_STOP_LOSS"
        assert order["exit_reason"] == "STOP_LOSS" and order["exit_price"] == 86.41
        assert order["low_price"] == 86.41
    asyncio.run(run())



def test_supertrend_opposite_signal_closes_and_replaces_open_order(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        isolation_id = "2026-11-12__CE"
        await engine.register_isolation(isolation_id, isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})

        # Resistance is the bearish setup and enters on the opposite leg.
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "RESISTANCE",
            "confirmed_time": "2026-11-12T09:40:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        first = store.load(isolation_id)["orders"]["supertrend"]
        assert first["target_instrument_key"] == "PE"
        assert first["status"] == "DUMMY_PLACED"

        # A later support signal is bullish and routes to the isolated leg.
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "SUPPORT",
            "confirmed_time": "2026-11-12T09:45:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)

        state = store.load(isolation_id)
        replacement = state["orders"]["supertrend"]
        closed = state["order_history"][-1]
        assert closed["order_id"] == first["order_id"]
        assert closed["status"] == "COMPLETED_SIGNAL_REVERSAL"
        assert closed["exit_reason"] == "SIGNAL_REVERSAL"
        assert closed["exit_price"] == 123.45
        assert replacement["target_instrument_key"] == "CE"
        assert replacement["target_leg_role"] == "isolated"
        assert replacement["status"] == "DUMMY_PLACED"
        assert replacement["order_id"] != first["order_id"]

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
                return {"received_at": "2026-11-12T09:50:00+05:30",
                        "data": {"ltpc": {"ltp": price}}}

        config = SimpleNamespace(
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
            "confirmed_time": "2026-11-12T09:40:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "SUPPORT",
            "confirmed_time": "2026-11-12T09:45:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        state = store.load(isolation_id)
        assert state["orders"]["supertrend"]["status"] == "COMPLETED_TARGET"
        assert state["orders"]["supertrend"]["target_instrument_key"] == "PE"
        assert len(state.get("order_history", [])) == 1

    asyncio.run(run())



def test_startup_recovery_replays_stop_then_target_and_locks_day(tmp_path):
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
                    {"ts": "2026-11-12T09:45:00+05:30", "open": 101, "high": 151, "low": 100, "close": 150},
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
                {"signal": "BULLISH", "confirmed_time": "2026-11-12T09:43:00+05:30"},
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
            "confirmed_time": "2026-11-12T09:40:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        first = store.load(isolation_id)["orders"]["supertrend"]
        assert first["order_sequence"] == 1

        # A target closes entry 1; a later signal may still create entry 2.
        first_tick = (datetime.fromisoformat(first["started_at_ist"]) + timedelta(seconds=60)).isoformat()
        await engine.on_market_tick("CE", {"ltpc": {"ltp": 173.45}}, first_tick)
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BEARISH",
            "confirmed_time": "2026-11-12T09:45:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)
        second = store.load(isolation_id)["orders"]["supertrend"]
        assert second["order_sequence"] == 2
        assert second["target_instrument_key"] == "PE"
        assert store.load(isolation_id)["order_history"][0]["exit_reason"] == "TARGET"

        # Close entry 2. A third confirmed signal must not create another order.
        second_tick = (datetime.fromisoformat(second["started_at_ist"]) + timedelta(seconds=60)).isoformat()
        await engine.on_market_tick("PE", {"ltpc": {"ltp": 86.41}}, second_tick)
        await engine.on_supertrend_frame({
            "event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:50:00+05:30", "price": 123.45,
        }, "CE", "isolated", isolation_id)

        state = store.load(isolation_id)
        current = state["orders"]["supertrend"]
        assert current["order_id"] == second["order_id"]
        assert current["status"] == "COMPLETED_STOP_LOSS"
        assert state["supertrend_order_count"] == 2
        assert state["supertrend_order_limit"] == 2
        assert len(state["order_history"]) == 2
        assert [item["order_sequence"] for item in state["order_history"]] == [1, 2]

    asyncio.run(run())
