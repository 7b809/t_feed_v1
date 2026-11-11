# app/api/sr.py
"""
SR REST + WebSocket API.

Config format: "<left_bars>/<right_bars>" — see sr_calculator.py.

REST
  GET  /api/v1/sr/configs
  GET  /api/v1/sr/status
  GET  /api/v1/sr/{instrument_key}/latest?config=15/3
  POST /api/v1/sr/subscribe/{instrument_key}?config=15/3
  POST /api/v1/sr/unsubscribe/{instrument_key}?config=15/3

WebSocket
  /ws/sr/{instrument_key}?config=15/3      (or config=all for the 3 defaults)
  /ws/sr/index/{underlying}?config=15/3    (underlying alias)

Connection handshake (WebSocket)
--------------------------------
On every new WebSocket connection the server sends, in this order:

  1.  ``event: "connection_ack"``
        Confirms the socket is live and tells the client that the feed
        operates in *today-only* SR mode:

            {
              "event":          "connection_ack",
              "status":         "connected",
              "message":        "Feed connected successfully",
              "data_scope":     "today_only",
              "today_ist_date": "YYYY-MM-DD",   <-- current IST date
              "scope_note":     "For further reference, only today's SR levels are used",
              "instrument_key": "<key>",
              "configs":        ["15/3", ...]
            }

        ``today_ist_date`` is present on the very first frame so a
        well-behaved client can verify the today-only scope immediately
        — it does not have to wait for the first ``snapshot``.

  2.  One ``event: "snapshot"`` frame per config, carrying only levels
      whose pivot bar was formed today (IST). Snapshot also embeds the
      same ``feed_status`` / ``feed_message`` / ``data_scope`` /
      ``today_ist_date`` / ``scope_note`` fields so a stateless client
      that missed the connection_ack still sees the policy.

  3.  Live frames (``heartbeat`` every SR_HEARTBEAT_CYCLES cycles,
      ``sr_update`` whenever the today-only level set changes) — all
      tagged with ``"data_scope": "today_only"`` and
      ``"today_ist_date": "YYYY-MM-DD"``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from app.services.sr_calculator import (
    DEFAULT_CONFIGS,
    SrCalculationError,
    config_slug,
    parse_config,
)
from app.services.sr_feed_service import SrFeedError

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

router = APIRouter(tags=["sr"])


def _now_ist_date_iso() -> str:
    """Current IST date as 'YYYY-MM-DD' — used for the today-only scope marker."""
    return datetime.now(IST).date().isoformat()


def _service(request: Request):
    svc = getattr(request.app.state, "sr_feed_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="SR service unavailable")
    return svc


def _normalize_config_or_400(config: str) -> str:
    try:
        left_bars, right_bars = parse_config(config)
    except SrCalculationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return f"{left_bars}/{right_bars}"


async def _reject_ws(websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        await websocket.accept()
    except Exception:  # noqa: BLE001
        pass
    try:
        await websocket.close(code=code, reason=reason[:120])
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@router.get("/api/v1/sr/configs")
def sr_configs(request: Request) -> dict[str, Any]:
    svc = _service(request)
    return {
        "defaults": svc.default_configs(),
        "canonical": list(DEFAULT_CONFIGS),
        "format": "<left_bars>/<right_bars>",
        "examples": ["15/3", "15/15", "15/30"],
        "data_scope": "today_only",
        "today_ist_date": _now_ist_date_iso(),
        "scope_note": "For further reference, only today's SR levels are used",
        "notes": {
            "left_bars": "bars to the LEFT of the pivot to compare against",
            "right_bars": (
                "bars to the RIGHT to confirm the pivot (confirmation delay); "
                "small = reactive, large = strong"
            ),
        },
    }


@router.get("/api/v1/sr/status")
def sr_status(request: Request) -> dict[str, Any]:
    return _service(request).status()


@router.get("/api/v1/sr/{instrument_key:path}/latest")
def sr_latest(
    request: Request,
    instrument_key: str,
    config: str = Query("all", description="'15/3' or 'all'"),
) -> dict[str, Any]:
    svc = _service(request)
    canonical_key = svc.canonical_key(instrument_key)

    if config.strip().lower() == "all":
        result: dict[str, Any] = {
            "instrument_key": canonical_key,
            "data_scope": "today_only",
            "today_ist_date": _now_ist_date_iso(),
            "scope_note": "For further reference, only today's SR levels are used",
            "configs": {},
        }
        for cfg in svc.default_configs():
            snap = svc.snapshot(canonical_key, cfg)
            if snap is not None:
                result["configs"][config_slug(cfg)] = snap
        if not result["configs"]:
            raise HTTPException(
                status_code=404,
                detail=f"No SR snapshot yet for {canonical_key}",
            )
        return result

    canonical_cfg = _normalize_config_or_400(config)
    snap = svc.snapshot(canonical_key, canonical_cfg)
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail=f"No SR snapshot yet for {canonical_key} / {canonical_cfg}",
        )
    return snap


@router.post("/api/v1/sr/subscribe/{instrument_key:path}")
async def sr_subscribe(
    request: Request,
    instrument_key: str,
    config: str = Query(..., description="'15/3' or 'all'"),
) -> dict[str, Any]:
    svc = _service(request)
    if config.strip().lower() == "all":
        results = []
        for cfg in svc.default_configs():
            try:
                results.append(await svc.subscribe(instrument_key, cfg))
            except SrFeedError as exc:
                results.append({"config": cfg, "error": str(exc)})
        return {
            "instrument_key": svc.canonical_key(instrument_key),
            "data_scope": "today_only",
            "today_ist_date": _now_ist_date_iso(),
            "results": results,
        }

    canonical_cfg = _normalize_config_or_400(config)
    try:
        return await svc.subscribe(instrument_key, canonical_cfg)
    except SrFeedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.post("/api/v1/sr/unsubscribe/{instrument_key:path}")
async def sr_unsubscribe(
    request: Request,
    instrument_key: str,
    config: str = Query(..., description="'15/3' or 'all'"),
) -> dict[str, Any]:
    svc = _service(request)
    if config.strip().lower() == "all":
        removed = []
        for cfg in svc.default_configs():
            removed.append(
                {
                    "config": cfg,
                    "removed": await svc.unsubscribe(instrument_key, cfg),
                }
            )
        return {"instrument_key": svc.canonical_key(instrument_key), "results": removed}

    canonical_cfg = _normalize_config_or_400(config)
    ok = await svc.unsubscribe(instrument_key, canonical_cfg)
    return {
        "instrument_key": svc.canonical_key(instrument_key),
        "config": canonical_cfg,
        "removed": ok,
    }


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
async def _serve_sr_ws(
    websocket: WebSocket,
    instrument_key: str,
    configs: list[str],
    svc,
) -> None:
    canonical_key = svc.canonical_key(instrument_key)

    subscribed: list[str] = []
    for cfg in configs:
        try:
            await svc.subscribe(canonical_key, cfg)
            subscribed.append(config_slug(cfg))
        except SrFeedError as exc:
            logger.warning(
                "SR auto-subscribe failed for %s / %s: %s",
                canonical_key, cfg, exc,
            )

    if not subscribed:
        await _reject_ws(
            websocket,
            4404,
            f"Could not subscribe to {canonical_key} for any requested config",
        )
        return

    channels = [f"sr:{canonical_key}:{cfg}" for cfg in subscribed]
    await websocket.accept()
    for ch in channels:
        await svc.broadcaster.register(ch, websocket)

    # ------------------------------------------------------------------ #
    # 1) Connection acknowledgement  (sent ONCE, before any snapshot)
    #
    #    Carries today_ist_date so a well-behaved client can verify the
    #    today-only scope on the very first frame — it does not have to
    #    wait for the snapshot.
    # ------------------------------------------------------------------ #
    await websocket.send_json({
        "event": "connection_ack",
        "status": "connected",
        "message": "Feed connected successfully",
        "instrument_key": canonical_key,
        "configs": subscribed,
        "data_scope": "today_only",
        "today_ist_date": _now_ist_date_iso(),
        "scope_note": "For further reference, only today's SR levels are used",
    })

    # ------------------------------------------------------------------ #
    # 2) Today-only snapshot, one frame per config
    # ------------------------------------------------------------------ #
    for cfg in subscribed:
        snap = svc.snapshot(canonical_key, cfg)
        if snap is not None:
            await websocket.send_json({"event": "snapshot", **snap})

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("SR WS closed unexpectedly for %s", canonical_key)
    finally:
        for ch in channels:
            await svc.broadcaster.unregister(ch, websocket)


@router.websocket("/ws/sr/index/{underlying}")
async def ws_sr_index(
    websocket: WebSocket, underlying: str, config: str | None = None
) -> None:
    svc = websocket.app.state.sr_feed_service
    raw = websocket.query_params.get("config", "all")
    if raw.strip().lower() == "all":
        configs = svc.default_configs()
    else:
        try:
            left_bars, right_bars = parse_config(raw)
        except SrCalculationError as exc:
            await _reject_ws(websocket, 4400, str(exc))
            return
        configs = [f"{left_bars}/{right_bars}"]

    await _serve_sr_ws(websocket, underlying, configs, svc)


@router.websocket("/ws/sr/{instrument_key:path}")
async def ws_sr_instrument(websocket: WebSocket, instrument_key: str) -> None:
    svc = websocket.app.state.sr_feed_service
    raw = websocket.query_params.get("config")
    if not raw:
        await _reject_ws(websocket, 4400, "missing ?config=15/3 (or ?config=all)")
        return

    if raw.strip().lower() == "all":
        configs = svc.default_configs()
    else:
        try:
            left_bars, right_bars = parse_config(raw)
        except SrCalculationError as exc:
            await _reject_ws(websocket, 4400, str(exc))
            return
        configs = [f"{left_bars}/{right_bars}"]

    await _serve_sr_ws(websocket, instrument_key, configs, svc)