"""
Configuration for the isolation layer + order placement.

All flags are read from the environment. Defaults are safe: order
placement is off, dry-run is on, sell-on-bearish is off, and isolation
scope defaults to per-index (each enabled index gets its own winner).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _b(name: str, default: bool) -> bool:
    return str(os.getenv(name, str(default))).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _s(name: str, default: str) -> str:
    return str(os.getenv(name, default) or default).strip()


@dataclass(frozen=True)
class IsolationConfig:
    # ------------------------------------------------------------------
    # Isolation master switch
    # ------------------------------------------------------------------
    enabled: bool = _b("EMA_ISOLATION_ENABLED", True)

    # ------------------------------------------------------------------
    # Isolation scope
    # ------------------------------------------------------------------
    # false (default) -> per-index isolation.
    #     Each enabled index has its own candidate pool and its own
    #     winner. NIFTY's touches never compete with SENSEX's touches.
    #     Up to N winners per session, one per enabled index.
    #
    # true -> global isolation.
    #     All enabled indexes share ONE candidate pool and ONE winner
    #     per session. Selection priority (r3/s3 > r2/s2 > r1/s1),
    #     then earliest touch, then smallest distance to the index LTP
    #     — evaluated across the union of NIFTY + SENSEX (and any other
    #     enabled index) contracts.
    #
    #     The winner still carries its real `underlying` (NIFTY/SENSEX),
    #     so budget filter, index LTP lookup, and reference average all
    #     stay scoped to that index — only the candidate pool is shared.
    scope_global: bool = _b("EMA_ISOLATION_SCOPE_GLOBAL", False)

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------
    # Master switch for order placement. When false, alerts are emitted
    # but no order is ever placed.
    orders_enabled: bool = _b("EMA_ALERT_ORDERS_ENABLED", False)

    # THE KEY FLAG:
    #   true  -> place market order on the isolated instrument itself on
    #            its EMA cross (bullish => BUY, bearish => SELL if
    #            `place_sell_on_bearish` is true, otherwise skip)
    #   false -> place market orders on the budget-filtered instruments
    #            from the alert payload (up to `max_orders_per_alert`)
    isolated_instrument_orders_only: bool = _b(
        "ISOLATED_INSTRUMENT_ORDERS_ONLY", True
    )

    # When in isolated-only mode and the cross is bearish, should we
    # still place a SELL order for the isolated instrument?
    # Default false: a bearish cross on a long CE means "exit", not
    # "short", so we skip.
    place_sell_on_bearish: bool = _b(
        "ISOLATED_INSTRUMENT_PLACE_SELL_ON_BEARISH", False
    )

    # When in budget mode, how many budget instruments to order per alert.
    max_orders_per_alert: int = _i("EMA_ALERT_MAX_ORDERS_PER_ALERT", 2)

    # ------------------------------------------------------------------
    # Order defaults
    # ------------------------------------------------------------------
    default_quantity: int = _i("EMA_ALERT_ORDER_DEFAULT_QUANTITY", 65)
    product: str = _s("EMA_ALERT_ORDER_PRODUCT", "D")
    validity: str = _s("EMA_ALERT_ORDER_VALIDITY", "DAY")
    tag: str = _s("EMA_ALERT_ORDER_TAG", "EMA_ISOLATED")
    transaction_type: str = _s("EMA_ALERT_ORDER_TRANSACTION_TYPE", "BUY")

    # Dry-run: build the intended orders, log them, but do NOT call the
    # Upstox API. Use this to validate the pipeline before going live.
    dry_run: bool = _b("EMA_ALERT_ORDER_DRY_RUN", True)

    # ------------------------------------------------------------------
    # Duplicate protection (per instrument per minute per direction)
    # ------------------------------------------------------------------
    dedupe_window_sec: int = _i("EMA_ALERT_ORDER_DEDUPE_WINDOW_SEC", 60)

    # ------------------------------------------------------------------
    # Persistence (MongoDB) — Orders
    # ------------------------------------------------------------------
    # One document per trading day (`_id = YYYY-MM-DD`), orders stored as
    # sub-documents keyed `HH_MM_SS` (with `_N` suffix on same-second
    # collisions). Each order carries `alert_key` — a cross-reference
    # into the matching day's `alerts.<key>` in the alert collection.
    storage_enabled: bool = _b("EMA_ALERT_ORDER_STORAGE_ENABLED", True)
    storage_fail_open: bool = _b("EMA_ALERT_ORDER_STORAGE_FAIL_OPEN", True)
    storage_collection: str = _s(
        "EMA_ALERT_ORDER_STORAGE_COLLECTION", "ema_isolated_orders"
    )

    # ------------------------------------------------------------------
    # Persistence (MongoDB) — Alerts
    # ------------------------------------------------------------------
    # One document per trading day (`_id = YYYY-MM-DD`), alerts stored as
    # sub-documents keyed `HH_MM_SS` (with `_N` suffix on same-second
    # collisions). Written BEFORE orders so orders can cross-reference.
    # Fail-open: a Mongo failure never blocks order placement.
    alert_storage_enabled: bool = _b(
        "EMA_ISOLATED_ALERT_STORAGE_ENABLED", True
    )
    alert_storage_fail_open: bool = _b(
        "EMA_ISOLATED_ALERT_STORAGE_FAIL_OPEN", True
    )
    alert_storage_collection: str = _s(
        "EMA_ISOLATED_ALERT_STORAGE_COLLECTION", "ema_isolated_alerts"
    )

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------
    telegram_enabled: bool = _b(
        "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED", True
    )


isolation_config = IsolationConfig()