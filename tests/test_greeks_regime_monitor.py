# tests/test_greeks_regime_monitor.py
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.greeks_regime_monitor import GreeksRegimeMonitor
from services.live_feed_service import LiveFeedService

IST = ZoneInfo("Asia/Kolkata")


def option_entry(key="NSE_FO|12345", strike_type="CE", strike=24000):
    return {
        "instrument_key": key,
        "trading_symbol": f"NIFTY {strike} {strike_type}",
        "underlying": "NIFTY",
        "strike_price": strike,
        "strike_type": strike_type,
        "expiry": "2026-11-19",
        "is_index": False,
    }


def option_tick(delta, rho, premium, *, up=24720.0, iv=12.4, op=None):
    return {
        "ltp": premium,
        "prev_close": 100.0,
        "volume": 1000,
        "candle_1m": {"close": premium},
        "option_full_feed": True,
        "option_greeks": {
            "delta": delta, "theta": -18.5, "gamma": 0.001,
            "vega": 10.7, "rho": rho, "up": up, "iv": iv,
            "op": premium if op is None else op,
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

    def warm_call(self, entry, *, up=24720.0):
        for minute, delta in [
            (15, .40), (16, .42), (17, .41),
            (18, .40), (19, .42), (20, .41),
        ]:
            self.monitor.on_tick(
                entry, option_tick(delta, 1.2, 100 + minute, up=up),
                self.at(minute),
            )

    def test_records_full_feed_snapshot_with_moneyness_once_per_minute(self):
        entry = option_entry()
        self.warm_call(entry)
        event = self.monitor.on_tick(
            entry, option_tick(.80, 1.5, 106), self.at(21),
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["feed_source"], "OPTION_FULL")
        self.assertEqual(event["verdict"], "BULLISH")
        self.assertAlmostEqual(event["moneyness"], 24720.0 / 24000.0)
        self.assertEqual(event["option_feed"]["up"], 24720.0)
        self.assertEqual(event["option_feed"]["iv"], 12.4)
        self.assertEqual(event["option_feed"]["op"], 106.0)

        self.monitor.on_tick(
            entry, option_tick(.85, 1.55, 107),
            datetime(2026, 11, 16, 9, 21, 35, tzinfo=IST),
        )
        files = list(Path(self.temp_dir.name).glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        rows = [json.loads(line) for line in files[0].read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["greeks"]["gamma"], 0.001)

    def test_rejects_ltpc_only_even_if_greeks_are_present(self):
        entry = option_entry()
        tick = option_tick(.80, 1.5, 106)
        tick["option_full_feed"] = False
        self.assertIsNone(self.monitor.on_tick(entry, tick, self.at(15)))
        self.assertEqual(list(Path(self.temp_dir.name).glob("*.jsonl")), [])

    def test_put_uses_side_signed_delta_and_spot_moneyness(self):
        entry = option_entry(
            key="NSE_FO|12347", strike_type="PE", strike=24000,
        )
        for minute, delta in zip(
            range(15, 21), [-.40, -.42, -.41, -.40, -.42, -.41],
        ):
            self.monitor.on_tick(
                entry, option_tick(delta, -1.2, 100 + minute, up=23200),
                self.at(minute),
            )
        # Put delta moves toward zero: z_delta positive, z_bull negative.
        event = self.monitor.on_tick(
            entry, option_tick(-.02, -1.5, 105, up=23200), self.at(21),
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["side"], "PUT")
        self.assertEqual(event["verdict"], "BEARISH")
        self.assertAlmostEqual(event["moneyness"], 23200 / 24000)

    def test_moneyness_neutral_band_suppresses_delta_only_direction(self):
        entry = option_entry()
        self.warm_call(entry, up=24000)
        event = self.monitor.on_tick(
            entry, option_tick(.80, 1.5, 106, up=24000), self.at(21),
        )
        self.assertIsNone(event)

    def test_delta_only_fallback_when_spot_is_unavailable(self):
        entry = option_entry()
        self.warm_call(entry)
        event = self.monitor.on_tick(
            entry, option_tick(.80, 1.5, 106, up=None), self.at(21),
        )
        self.assertIsNotNone(event)
        self.assertIsNone(event["moneyness"])

    def test_ignores_non_options_and_mismatched_side(self):
        index_entry = option_entry(strike_type="")
        self.assertIsNone(
            self.monitor.on_tick(
                index_entry, option_tick(.8, 1.5, 100), self.at(15),
            )
        )
        put_entry = option_entry(key="NSE_FO|12346", strike_type="PE")
        self.assertIsNone(
            self.monitor.on_tick(
                put_entry, option_tick(.8, 1.5, 100), self.at(15),
            )
        )
        self.assertEqual(list(Path(self.temp_dir.name).glob("*.jsonl")), [])

    def test_portable_mode_requires_full_warmup(self):
        entry = option_entry()
        for minute, delta in [(15, .40), (16, .42), (17, .41), (18, .40)]:
            self.assertIsNone(
                self.monitor.on_tick(
                    entry, option_tick(delta, 1.2, 100 + minute),
                    self.at(minute),
                )
            )
        self.assertEqual(list(Path(self.temp_dir.name).glob("*.jsonl")), [])

    def test_live_parser_marks_only_option_full_shape_as_eligible(self):
        service = LiveFeedService()
        option_full = {
            "oc": {
                "ltpc": {"ltp": 106.0, "cp": 100.0, "ltt": "1794800700000"},
                "optionGreeks": {
                    "op": 106.0, "up": 24720.0, "iv": 12.4,
                    "delta": 0.8, "theta": -18.5, "gamma": 0.001,
                    "vega": 10.7, "rho": 1.5,
                },
                "eFeedDetails": {"vtt": 1000},
            }
        }
        parsed = service._extract_tick(option_full)
        self.assertTrue(parsed["option_full_feed"])
        self.assertEqual(parsed["option_greeks"]["up"], 24720.0)
        self.assertEqual(parsed["option_greeks"]["iv"], 12.4)
        self.assertEqual(parsed["option_greeks"]["op"], 106.0)

        ltpc_only = {"ltpc": {"ltp": 106.0, "cp": 100.0, "ltt": "1794800700000"}}
        parsed_ltpc = service._extract_tick(ltpc_only)
        self.assertFalse(parsed_ltpc["option_full_feed"])
        self.assertIsNone(parsed_ltpc["option_greeks"])

    def test_daily_baseline_resets(self):
        entry = option_entry()
        self.warm_call(entry)
        next_day = datetime(2026, 11, 17, 9, 15, tzinfo=IST)
        self.assertIsNone(
            self.monitor.on_tick(
                entry, option_tick(.8, 1.5, 100), next_day,
            )
        )
        self.assertEqual(self.monitor.status()["tracked_options"], 1)


if __name__ == "__main__":
    unittest.main()
