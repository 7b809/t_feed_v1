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
        dummy_orders_enabled=True, dummy_sr_orders_enabled=True,
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


def test_resistance_routes_opposite_and_is_idempotent(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        await engine.register_isolation("2026-11-12__CE", isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        event = {"event": "sr_update", "config": "15/3", "levels": [
            {"type": "RESISTANCE", "price": 216.6,
             "confirmed_time": "2026-11-12T09:41:00+05:30"}]}
        await engine.on_sr_frame(event, "CE", "isolated", "2026-11-12__CE")
        await engine.on_sr_frame(event, "CE", "isolated", "2026-11-12__CE")
        order = store.load("2026-11-12__CE")["orders"]["sr"]
        assert order["target_instrument_key"] == "PE"
        assert order["trading_symbol"] == "NIFTY 25000 PE"
        assert order["entry_price"] == 123.45
        assert order["stop_loss_price"] == 86.42
        assert order["target_points"] == 50
        assert order["target_price"] == 173.45
        assert order["real_order_submitted"] is False
    asyncio.run(run())


def test_target_point_bands(tmp_path):
    async def run():
        engine, _ = build(tmp_path)
        assert engine._target_points(250) == 60
        assert engine._target_points(150) == 50
        assert engine._target_points(100) == 50
        assert engine._target_points(99.99) == 40
    asyncio.run(run())


def test_disabled_sr_flag_is_persisted_locally_and_does_not_block_supertrend(tmp_path):
    async def run():
        engine, store = build(tmp_path, dummy_sr_orders_enabled=False)
        await engine.register_isolation("2026-11-12__CE", isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        state = store.load("2026-11-12__CE")
        assert state["orders"]["sr"]["status"] == "DISABLED"
        assert state["orders"]["sr"]["reason"] == "orders disabled due to flag false"
        await engine.on_sr_frame({"event": "sr_update", "supports": [{
            "price": 100, "confirmed_at": "2026-11-12T09:35:00+05:30"}]},
            "CE", "isolated", "2026-11-12__CE")
        await engine.on_supertrend_frame({"event": "supertrend_signal", "signal": "BULLISH",
            "confirmed_time": "2026-11-12T09:40:00+05:30", "price": 110},
            "CE", "isolated", "2026-11-12__CE")
        state = store.load("2026-11-12__CE")
        assert state["orders"]["sr"]["status"] == "DISABLED"
        assert state["orders"]["supertrend"]["target_instrument_key"] == "CE"
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


def test_signals_from_opposite_leg_are_ignored(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        await engine.register_isolation("2026-11-12__CE", isolation_payload(),
                                        {"isolated": "CE", "opposite": "PE"})
        await engine.on_sr_frame({"event": "sr_update", "supports": [
            {"price": 100, "confirmed_at": "2026-11-12T09:35:00+05:30"}]},
            "PE", "opposite", "2026-11-12__CE")
        assert store.load("2026-11-12__CE")["orders"]["sr"] is None
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
