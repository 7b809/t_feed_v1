from __future__ import annotations
import asyncio
from types import SimpleNamespace
from app.services.dummy_order_engine import DummyOrderEngine
from app.services.dummy_order_store import DummyOrderStore

class FakeFeed:
    def latest(self, key):
        return {"received_at": "2026-11-12T09:41:01+05:30", "data": {"ltpc": {"ltp": 123.45}}}

class FakeTelegram:
    def __init__(self): self.messages = []
    async def send_message(self, title, message, level="INFO"):
        self.messages.append((title, message, level))
        return True

def build(tmp_path):
    config = SimpleNamespace(dummy_orders_enabled=True, dummy_order_quantity=1,
                             dummy_order_state_dir=str(tmp_path))
    store, telegram = DummyOrderStore(config), FakeTelegram()
    return DummyOrderEngine(config, store, FakeFeed(), telegram), store

def test_resistance_routes_opposite_and_is_idempotent(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        await engine.register_isolation("iso", {"isolation_time":"2026-11-12T09:30:00+05:30"},
                                        {"isolated":"CE","opposite":"PE"})
        event = {"event":"sr_update","config":"15/3","levels":[
            {"type":"RESISTANCE","price":216.6,"confirmed_time":"2026-11-12T09:41:00+05:30"}]}
        await engine.on_sr_frame(event,"CE","isolated","iso")
        await engine.on_sr_frame(event,"CE","isolated","iso")
        order = store.load("iso")["orders"]["sr"]
        assert order["target_instrument_key"] == "PE"
        assert order["side"] == "BUY" and order["real_order_submitted"] is False
        assert order["entry_price"] == 123.45
    asyncio.run(run())

def test_support_routes_isolated(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        await engine.register_isolation("iso", {"isolation_time":"2026-11-12T09:30:00+05:30"},
                                        {"isolated":"CE","opposite":"PE"})
        await engine.on_sr_frame({"event":"sr_update","supports":[
            {"price":100,"confirmed_at":"2026-11-12T09:35:00+05:30"}]},
            "CE","isolated","iso")
        assert store.load("iso")["orders"]["sr"]["target_instrument_key"] == "CE"
    asyncio.run(run())

def test_supertrend_bullish_routes_same_leg(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        await engine.register_isolation("iso", {"isolation_time":"2026-11-12T09:30:00+05:30"},
                                        {"isolated":"CE","opposite":"PE"})
        await engine.on_supertrend_frame({"event":"supertrend_signal","signal":"BULLISH",
            "confirmed_time":"2026-11-12T09:40:00+05:30","price":110},"CE","isolated","iso")
        assert store.load("iso")["orders"]["supertrend"]["target_instrument_key"] == "CE"
    asyncio.run(run())

def test_pre_isolation_signal_is_ignored(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        await engine.register_isolation("iso", {"isolation_time":"2026-11-12T09:40:00+05:30"},
                                        {"isolated":"CE","opposite":"PE"})
        await engine.on_supertrend_frame({"event":"supertrend_signal","signal":"BEARISH",
            "confirmed_time":"2026-11-12T09:39:00+05:30","price":110},"CE","isolated","iso")
        assert store.load("iso")["orders"]["supertrend"] is None
    asyncio.run(run())

def test_supertrend_bearish_routes_opposite(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        await engine.register_isolation("iso", {"isolation_time":"2026-11-12T09:30:00+05:30"},
                                        {"isolated":"CE","opposite":"PE"})
        await engine.on_supertrend_frame({"event":"supertrend_signal","signal":"BEARISH",
            "confirmed_time":"2026-11-12T09:40:00+05:30","price":110},"CE","isolated","iso")
        assert store.load("iso")["orders"]["supertrend"]["target_instrument_key"] == "PE"
    asyncio.run(run())

def test_signals_from_opposite_leg_are_ignored(tmp_path):
    async def run():
        engine, store = build(tmp_path)
        await engine.register_isolation("iso", {"isolation_time":"2026-11-12T09:30:00+05:30"},
                                        {"isolated":"CE","opposite":"PE"})
        await engine.on_sr_frame({"event":"sr_update","supports":[
            {"price":100,"confirmed_at":"2026-11-12T09:35:00+05:30"}]},
            "PE","opposite","iso")
        assert store.load("iso")["orders"]["sr"] is None
    asyncio.run(run())
