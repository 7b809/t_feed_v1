"""
EMA crossover service.

- 9/21 EMA by default, on close prices of the stored candle file.
- Only crossover candles are saved (not every candle).
- Two outputs per contract:
    data/runtime/<INDEX>/<strike>_<CE|PE>/historic_cross.json
    data/runtime/<INDEX>/<strike>_<CE|PE>/intraday_cross.json
- Reads the readonly candle file written by candle_service; splits it
  into historic (before today) and intraday (today) sections using the
  stored historical_count / intraday_count markers.

Crossover definition:
  bullish : EMA_fast crosses from <= EMA_slow to >  EMA_slow
  bearish : EMA_fast crosses from >= EMA_slow to <  EMA_slow
"""
import threading
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from core.config import core_config
from core.logger import get_logger
from upstox_app.candle.candle_storage import candle_storage
from upstox_app.candle.crossover_storage import crossover_storage
from upstox_app.common.config import upstox_config

logger = get_logger(__name__)


def _cfg(name, default):
    return getattr(upstox_config, name, default)


def _compute_ema(values: List[float], period: int) -> List[Optional[float]]:
    """
    Standard EMA with SMA seed for the first `period` values.
    Entries before index `period - 1` are None (not enough data).
    """
    if period < 1 or not values:
        return [None] * len(values)
    ema: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return ema
    alpha = 2.0 / (period + 1)
    seed = sum(values[:period]) / float(period)
    ema[period - 1] = seed
    for i in range(period, len(values)):
        ema[i] = alpha * values[i] + (1.0 - alpha) * ema[i - 1]
    return ema


class CrossoverService:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ---- core ----------------------------------------------------------
    def _detect(
        self,
        candles: List[List[Any]],
        ema_fast: List[Optional[float]],
        ema_slow: List[Optional[float]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        bullish: List[Dict[str, Any]] = []
        bearish: List[Dict[str, Any]] = []
        for i in range(1, len(candles)):
            ef_p, es_p = ema_fast[i - 1], ema_slow[i - 1]
            ef_c, es_c = ema_fast[i], ema_slow[i]
            if ef_p is None or es_p is None or ef_c is None or es_c is None:
                continue
            diff_prev = ef_p - es_p
            diff_curr = ef_c - es_c
            if diff_prev < 0 and diff_curr > 0:
                kind = "bullish"
            elif diff_prev > 0 and diff_curr < 0:
                kind = "bearish"
            else:
                continue

            candle = candles[i]
            try:
                entry = {
                    "type": kind,
                    "crossover_at": str(candle[0]),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": int(candle[5]) if len(candle) > 5 and candle[5] is not None else 0,
                    "diff_prev": round(diff_prev, 6),
                    "diff_curr": round(diff_curr, 6),
                }
            except (TypeError, ValueError, IndexError) as exc:
                logger.debug(f"Skipping malformed candle at index {i} | err={exc}")
                continue

            if kind == "bullish":
                bullish.append(entry)
            else:
                bearish.append(entry)
        return bullish, bearish

    def _build_payload(
        self,
        index_name: str,
        contract: Dict[str, Any],
        source: str,
        candles: List[List[Any]],
        unit: str,
        interval: str,
        ema_fast_period: int,
        ema_slow_period: int,
    ) -> Dict[str, Any]:
        base = {
            "index_name": index_name.upper(),
            "strike_price": contract.get("strike_price"),
            "option_type": contract.get("option_type") or contract.get("instrument_type"),
            "instrument_key": contract.get("instrument_key"),
            "expiry": contract.get("expiry"),
            "trading_symbol": contract.get("trading_symbol"),
            "source": source,
            "unit": unit,
            "interval": interval,
            "ema_fast": ema_fast_period,
            "ema_slow": ema_slow_period,
            "computed_at": datetime.now().astimezone().isoformat(),
        }

        if not candles:
            return {
                **base,
                "status": "empty",
                "candles_considered": 0,
                "from_date": None,
                "to_date": None,
                "bullish_count": 0,
                "bearish_count": 0,
                "crossovers": [],
            }

        closes = [float(c[4]) for c in candles]
        ema_fast = _compute_ema(closes, ema_fast_period)
        ema_slow = _compute_ema(closes, ema_slow_period)
        bullish, bearish = self._detect(candles, ema_fast, ema_slow)
        merged = sorted(bullish + bearish, key=lambda x: x["crossover_at"])

        return {
            **base,
            "status": "success",
            "candles_considered": len(candles),
            "from_date": str(candles[0][0])[:10],
            "to_date": str(candles[-1][0])[:10],
            "bullish_count": len(bullish),
            "bearish_count": len(bearish),
            "crossovers": merged,
        }

    # ---- public API ----------------------------------------------------
    def compute_for_contract(
        self, index_name: str, contract: Dict[str, Any]
    ) -> Dict[str, Any]:
        strike = contract.get("strike_price")
        otype = contract.get("option_type") or contract.get("instrument_type")
        result: Dict[str, Any] = {
            "instrument_key": contract.get("instrument_key"),
            "strike": strike,
            "type": otype,
            "historic": {"status": "skipped"},
            "intraday": {"status": "skipped"},
        }

        data = candle_storage.load(index_name, strike, otype)
        if not data:
            result["historic"] = {"status": "no_candle_file"}
            result["intraday"] = {"status": "no_candle_file"}
            return result

        all_candles = data.get("candles") or []
        hist_count = int(data.get("historical_count") or 0)
        intra_count = int(data.get("intraday_count") or 0)

        if hist_count + intra_count != len(all_candles):
            # Fallback: split by date (intraday = today's date prefix)
            today_iso = date.today().isoformat()
            historic_candles = [c for c in all_candles if str(c[0])[:10] < today_iso]
            intraday_candles = [c for c in all_candles if str(c[0])[:10] == today_iso]
        else:
            historic_candles = all_candles[:hist_count]
            intraday_candles = all_candles[hist_count:]

        unit = data.get("unit", "minutes")
        interval = data.get("interval", "1")
        fast = int(_cfg("CROSSOVER_EMA_FAST", 9))
        slow = int(_cfg("CROSSOVER_EMA_SLOW", 21))

        # Historic
        try:
            payload = self._build_payload(
                index_name, contract, "historic",
                historic_candles, unit, interval, fast, slow,
            )
            crossover_storage.save(index_name, strike, otype, "historic", payload)
            result["historic"] = {
                "status": payload["status"],
                "candles": payload["candles_considered"],
                "bullish": payload["bullish_count"],
                "bearish": payload["bearish_count"],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"Historic crossover failed | index={index_name} "
                f"| strike={strike} | type={otype} | err={exc}"
            )
            result["historic"] = {"status": "error", "error": str(exc)}

        # Intraday
        try:
            payload = self._build_payload(
                index_name, contract, "intraday",
                intraday_candles, unit, interval, fast, slow,
            )
            crossover_storage.save(index_name, strike, otype, "intraday", payload)
            result["intraday"] = {
                "status": payload["status"],
                "candles": payload["candles_considered"],
                "bullish": payload["bullish_count"],
                "bearish": payload["bearish_count"],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"Intraday crossover failed | index={index_name} "
                f"| strike={strike} | type={otype} | err={exc}"
            )
            result["intraday"] = {"status": "error", "error": str(exc)}

        return result

    def calculate_index(self, index_name: str) -> Dict[str, Any]:
        index_name = index_name.upper()
        from upstox_app.candle.candle_service import candle_service
        contracts = candle_service._load_contracts(index_name)

        summary: Dict[str, Any] = {
            "index_name": index_name,
            "total_contracts": len(contracts),
            "historic_success": 0,
            "historic_empty": 0,
            "intraday_success": 0,
            "intraday_empty": 0,
            "errors": 0,
        }
        if not contracts:
            logger.warning(f"Crossover | no contracts for index={index_name}")
            return summary

        for c in contracts:
            res = self.compute_for_contract(index_name, c)
            h = res.get("historic", {})
            i = res.get("intraday", {})
            if h.get("status") == "success":
                summary["historic_success"] += 1
            elif h.get("status") == "empty":
                summary["historic_empty"] += 1
            elif h.get("status") == "error":
                summary["errors"] += 1
            if i.get("status") == "success":
                summary["intraday_success"] += 1
            elif i.get("status") == "empty":
                summary["intraday_empty"] += 1
            elif i.get("status") == "error":
                summary["errors"] += 1

        logger.info(
            "Crossover summary | index=%s | total=%d | historic_ok=%d historic_empty=%d "
            "| intraday_ok=%d intraday_empty=%d | errors=%d",
            index_name, summary["total_contracts"],
            summary["historic_success"], summary["historic_empty"],
            summary["intraday_success"], summary["intraday_empty"],
            summary["errors"],
        )
        return summary

    def calculate_all_enabled(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "enabled": bool(_cfg("CROSSOVER_ENABLED", True)),
            "ema_fast": int(_cfg("CROSSOVER_EMA_FAST", 9)),
            "ema_slow": int(_cfg("CROSSOVER_EMA_SLOW", 21)),
            "indexes": {},
            "totals": {
                "indexes": 0,
                "total_contracts": 0,
                "historic_success": 0,
                "historic_empty": 0,
                "intraday_success": 0,
                "intraday_empty": 0,
                "errors": 0,
            },
        }
        if not result["enabled"]:
            logger.info("Crossover disabled; skipping")
            return result

        with self._lock:
            for name, meta in core_config.MAIN_INDEXES.items():
                if not meta.get("enabled"):
                    continue
                r = self.calculate_index(name)
                result["indexes"][name] = r
                result["totals"]["indexes"] += 1
                for k in (
                    "total_contracts", "historic_success", "historic_empty",
                    "intraday_success", "intraday_empty", "errors",
                ):
                    result["totals"][k] += r.get(k, 0)

        t = result["totals"]
        logger.info(
            "Crossover GLOBAL | indexes=%d | total=%d | historic_ok=%d historic_empty=%d "
            "| intraday_ok=%d intraday_empty=%d | errors=%d",
            t["indexes"], t["total_contracts"],
            t["historic_success"], t["historic_empty"],
            t["intraday_success"], t["intraday_empty"],
            t["errors"],
        )
        return result


crossover_service = CrossoverService()