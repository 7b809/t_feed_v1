# tests/test_greeks_regime_monitor.py
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.greeks_regime_monitor import GreeksRegimeMonitor

IST = ZoneInfo("Asia/Kolkata")

def option_entry(key="NSE_FO|12345", strike_type="CE"):
    return {
        "instrument_key": key, "trading_symbol": "NIFTY 24000 CE",
        "underlying": "NIFTY", "strike_price": 24000,
        "strike_type": strike_type, "expiry": "2026-11-19",
        "is_index": False,
    }

def option_tick(delta, rho, premium):
    return {
        "ltp": premium, "prev_close": 100.0, "volume": 1000,
        "candle_1m": {"close": premium},
        "option_greeks": {
            "delta": delta, "theta": -18.5, "gamma": 0.001,
            "vega": 10.7, "rho": rho,
        },
    }

class GreeksRegimeMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.monitor = GreeksRegimeMonitor(
            enabled=True, output_dir=Path(self.temp_dir.name),
            lookback=5, z_threshold=0.5, std_floor=0.0001,
            timezone_name="Asia/Kolkata",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def at(self, minute):
        return datetime(2026, 11, 16, 9, minute, tzinfo=IST)

    def test_records_first_qualifying_snapshot_once_per_option_minute(self):
        entry = option_entry()
        for minute, delta in [(15, .40), (16, .42), (17, .41), (18, .40), (19, .42), (20, .41)]:
            self.monitor.on_tick(entry, option_tick(delta, 1.2, 100 + minute), self.at(minute))
        event = self.monitor.on_tick(entry, option_tick(.80, 1.5, 106), self.at(21))
        self.assertIsNotNone(event)
        self.assertEqual(event["verdict"], "BULLISH")
        self.assertEqual(event["side"], "CALL")
        self.monitor.on_tick(entry, option_tick(.85, 1.55, 107), datetime(2026, 11, 16, 9, 21, 35, tzinfo=IST))
        files = list(Path(self.temp_dir.name).glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        rows = [json.loads(line) for line in files[0].read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["minute_bucket"], self.at(21).isoformat())
        self.assertEqual(rows[0]["greeks"]["gamma"], 0.001)

    def test_ignores_non_options_and_mismatched_side(self):
        index_entry = option_entry(strike_type="")
        self.assertIsNone(self.monitor.on_tick(index_entry, option_tick(.8, 1.5, 100), self.at(15)))
        put_entry = option_entry(key="NSE_FO|12346", strike_type="PE")
        self.assertIsNone(self.monitor.on_tick(put_entry, option_tick(.8, 1.5, 100), self.at(15)))
        self.assertEqual(list(Path(self.temp_dir.name).glob("*.jsonl")), [])

    def test_portable_mode_requires_full_warmup(self):
        entry = option_entry()
        for minute, delta in [(15, .40), (16, .42), (17, .41), (18, .40)]:
            self.assertIsNone(self.monitor.on_tick(entry, option_tick(delta, 1.2, 100 + minute), self.at(minute)))
        self.assertEqual(list(Path(self.temp_dir.name).glob("*.jsonl")), [])

    def test_put_z_score_direction_is_inverted(self):
        entry = option_entry(key="NSE_FO|12347", strike_type="PE")
        for minute, delta in zip(range(15, 21), [-.40, -.42, -.41, -.40, -.42, -.41]):
            self.monitor.on_tick(entry, option_tick(delta, -1.2, 100 + minute), self.at(minute))
        event = self.monitor.on_tick(entry, option_tick(-.80, -1.5, 105), self.at(21))
        self.assertIsNotNone(event)
        self.assertEqual(event["side"], "PUT")
        self.assertEqual(event["verdict"], "BEARISH")

if __name__ == "__main__":
    unittest.main()
