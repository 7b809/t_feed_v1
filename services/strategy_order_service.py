"""Stage 4 — selection + order placement.

At 09:18 IST (configurable), reads the stage-3 activations and:

  1. Keeps the N strikes per index nearest to that index's ATM strike
     (NIFTY: 3 by default, SENSEX: 5).
  2. Applies FIFO on R2-touch time across the combined pool.
  3. Places a paper order for the final pick and writes the order to::

         data/strategy/stage_4_order/<TICKER>/<INDEX_NAME>/orders.json

The order is a JSON record — the real Upstox order API is not called.
A ``note`` field records this so downstream tooling can distinguish
paper from live orders.

The service never raises; failures are reported in the summary.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import config
from services.live_feed_service import live_feed_service

logger = logging.getLogger(__name__)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrategyOrderService:
    # ------------------------------------------------------------------ #
    # Public entry                                                       #
    # ------------------------------------------------------------------ #
    def run(self, reason: str = "manual") -> dict[str, Any]:
        if not config.STRATEGY_ENABLED or not config.STRATEGY_ORDER_ENABLED:
            return {
                "status": "disabled",
                "stage": "stage_4",
                "reason": reason,
                "generated_at": _now_iso(),
                "placed": 0,
                "candidates": 0,
                "orders": [],
            }

        stage3_base = (
            Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_3_NAME
        )
        stage4_base = (
            Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_4_NAME
        )
        stage3_summary = _read_json(stage3_base / "stage_3_summary.json") or {}
        activations = stage3_summary.get("activated") or []

        if not activations:
            logger.info("[stage_4:%s] no stage-3 activations", reason)
            return {
                "status": "no_activations",
                "stage": "stage_4",
                "reason": reason,
                "generated_at": _now_iso(),
                "placed": 0,
                "candidates": 0,
                "orders": [],
            }

        today = datetime.now().date().isoformat()

        # ---- 1. Split by ticker -----------------------------------------
        by_ticker: dict[str, list[dict[str, Any]]] = {}
        for a in activations:
            t = str(a.get("ticker") or "").upper().strip()
            if t:
                by_ticker.setdefault(t, []).append(a)

        # ---- 2. Nearest-to-ATM per index --------------------------------
        picks: list[dict[str, Any]] = []
        picks.extend(
            self._pick_nearest(
                by_ticker.get("NIFTY", []),
                config.STRATEGY_ORDER_NIFTY_PICKS,
            )
        )
        picks.extend(
            self._pick_nearest(
                by_ticker.get("SENSEX", []),
                config.STRATEGY_ORDER_SENSEX_PICKS,
            )
        )

        # ---- 3. FIFO by touched_at --------------------------------------
        picks.sort(key=lambda p: str(p.get("touched_at") or ""))
        final = picks[: config.STRATEGY_ORDER_FINAL_PICKS]

        logger.info(
            "[stage_4:%s] candidates=%d -> picks=%d -> final=%d",
            reason, len(activations), len(picks), len(final),
        )

        # ---- 4. Place order for each final pick -------------------------
        orders: list[dict[str, Any]] = []
        for p in final:
            order = self._place_order(p, picks)
            orders.append(order)
            self._materialise_stage4(stage4_base, p, order)

        # ---- 5. Write summary -------------------------------------------
        summary = {
            "status": "ok",
            "stage": "stage_4",
            "reason": reason,
            "day": today,
            "generated_at": _now_iso(),
            "candidates": len(activations),
            "picks": len(picks),
            "placed": len(orders),
            "orders": orders,
        }
        try:
            stage4_base.mkdir(parents=True, exist_ok=True)
            _write_json(stage4_base / "stage_4_summary.json", summary)
        except Exception:
            logger.exception("[stage_4:%s] summary write failed", reason)

        return summary

    # ------------------------------------------------------------------ #
    # Selection helpers                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pick_nearest(
        pool: list[dict[str, Any]], limit: int,
    ) -> list[dict[str, Any]]:
        if not pool:
            return []
        def _dist(a: dict[str, Any]) -> float:
            try:
                sp = float(a.get("strike_price") or 0)
                atm = float(a.get("atm_strike") or sp)
                return abs(sp - atm)
            except (TypeError, ValueError):
                return float("inf")
        return sorted(pool, key=_dist)[: max(1, int(limit))]

    # ------------------------------------------------------------------ #
    # Order placement (paper)                                            #
    # ------------------------------------------------------------------ #
    def _place_order(
        self, pick: dict[str, Any], candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        instrument_key = pick.get("instrument_key")
        ltp = (
            live_feed_service.get_latest_ltp(
                pick.get("upstox_instrument_key") or ""
            )
            if pick.get("upstox_instrument_key")
            else None
        )
        if ltp is None and instrument_key:
            ltp = live_feed_service.get_latest_ltp(instrument_key)

        return {
            "day": pick.get("day"),
            "ticker": pick.get("ticker"),
            "index_name": pick.get("index_name"),
            "index_key": pick.get("index_key"),
            "instrument_key": instrument_key,
            "upstox_instrument_key": pick.get("upstox_instrument_key"),
            "strike_price": pick.get("strike_price"),
            "strike_type": pick.get("strike_type"),
            "side": "BUY",
            "quantity": config.STRATEGY_ORDER_QTY,
            "r2_value": pick.get("r2_value"),
            "index_ltp_at_touch": pick.get("index_ltp_at_touch"),
            "index_touched_at": pick.get("touched_at"),
            "selection_method": "nearest_to_atm_then_fifo",
            "selected_at": _now_iso(),
            "placed_at": _now_iso(),
            "entry_ltp": ltp,
            "status": "paper_filled",
            "note": (
                "Paper order — real Upstox order API is not wired in this "
                "build. Wire /v2/order/place here to go live."
            ),
            "candidates_before_fifo": [
                {
                    "instrument_key": c.get("instrument_key"),
                    "strike_price": c.get("strike_price"),
                    "strike_type": c.get("strike_type"),
                    "touched_at": c.get("touched_at"),
                }
                for c in candidates
            ],
        }

    # ------------------------------------------------------------------ #
    # Stage-4 materialisation                                            #
    # ------------------------------------------------------------------ #
    def _materialise_stage4(
        self,
        stage4_base: Path,
        pick: dict[str, Any],
        order: dict[str, Any],
    ) -> None:
        ticker = str(pick.get("ticker") or "UNKNOWN")
        index_name = str(pick.get("index_name") or "INDEX")
        strike_type = str(pick.get("strike_type") or "")
        strike_price = pick.get("strike_price")

        idx_out = stage4_base / ticker / index_name
        idx_out.mkdir(parents=True, exist_ok=True)

        # One orders.json per index folder, appended.
        orders_path = idx_out / "orders.json"
        existing = _read_json(orders_path) or {
            "day": order.get("day"),
            "ticker": ticker,
            "index_name": index_name,
            "orders": [],
        }
        existing["orders"].append(order)
        existing["updated_at"] = _now_iso()
        _write_json(orders_path, existing)

        # Copy the selected strike folder into stage 4.
        src = pick.get("stage_2_folder")
        if src:
            src_path = Path(src)
            dst = idx_out / f"{strike_price}_{strike_type}"
            try:
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src_path, dst)
                _write_json(dst / "order_meta.json", order)
            except Exception:
                logger.exception(
                    "[stage_4] failed copying %s -> %s", src_path, dst,
                )


strategy_order_service = StrategyOrderService()