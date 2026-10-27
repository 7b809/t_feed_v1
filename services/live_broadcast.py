# services/live_broadcast.py
"""In-process pub/sub hub for live ticks and live EMA crosses.

Each client registers a filter (instrument keys, or (underlying, strike,
type) tuples) and receives only matching payloads on two topics:

    topic "feed"    -> normalised last-tick snapshot per instrument
    topic "crosses" -> live EMA cross records

Only `crosses` are persisted; `feed` is ephemeral.

Both an async API (used by FastAPI route handlers) and a sync API (used
by worker threads running the Upstox WebSocket) are provided:

    await live_broadcast.publish(topic, entry, payload)   # async
    live_broadcast.publish_sync(topic, entry, payload)    # thread-safe

`publish_sync` requires that `set_loop(loop)` has been called once at
startup with the running asyncio loop. It schedules the coroutine on
that loop and returns immediately.

List-of-feeds support (2026-10-27)
----------------------------------
`resolve_subscription_params` now accepts either a single
``instrument_key`` (string) **or** a list of keys. The existing
``Subscriber.instrument_keys`` is already a ``set``, so the broadcast
fan-out was always list-capable — only the route-level parsing was
single-key. ``parse_instrument_list`` is the shared normaliser used by
every route that accepts a comma-separated ``instruments=`` query.

Comma-only parsing (2026-10-27 fix)
-----------------------------------
``parse_instrument_list`` splits **only on commas**. Whitespace is
never a separator because Upstox index keys legitimately contain
spaces, e.g. ``NSE_INDEX|Nifty 50`` and ``NSE_INDEX|Nifty Next 50``.
Earlier revisions split on whitespace too, which silently turned
``NSE_INDEX|Nifty 50`` into the two invalid keys ``NSE_INDEX|Nifty``
and ``50`` and caused downstream ``UDAPI100011`` /
``UDAPI1021`` errors in ``live_sr_calc_service``. Callers that need
multiple keys must send them comma-separated or as a list.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Subscriber:
    id: int
    queue: "asyncio.Queue[dict[str, Any]]"
    topic: str                               # "feed" | "crosses"
    instrument_keys: set[str] = field(default_factory=set)
    triples: set[tuple[str, str, str]] = field(default_factory=set)
    all_instruments: bool = False

    def matches(self, entry: dict[str, Any]) -> bool:
        if self.all_instruments:
            return True
        key = entry["instrument_key"]
        if key in self.instrument_keys:
            return True
        triple = (
            str(entry.get("underlying") or "").upper().replace(" ", ""),
            str(entry.get("strike_price") or ""),
            str(entry.get("strike_type") or "").upper(),
        )
        return triple in self.triples


class LiveBroadcast:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subs: dict[int, Subscriber] = {}
        self._next_id = 1
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ #
    # Loop wiring                                                        #
    # ------------------------------------------------------------------ #
    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store the running asyncio loop for sync publishing."""
        self._loop = loop

    # ------------------------------------------------------------------ #
    # Static helpers                                                     #
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_instrument_list(raw: Any) -> list[str]:
        """Normalise ``raw`` into a clean, de-duplicated list of keys.

        Accepts:

            None / ""                     -> []
            "NSE_FO|1,NSE_FO|2"           -> ["NSE_FO|1", "NSE_FO|2"]
            ["NSE_FO|1", "NSE_FO|2"]      -> ["NSE_FO|1", "NSE_FO|2"]
            ("NSE_FO|1", "NSE_FO|2", ...) -> same
            {..set..}                     -> same

        ONLY comma is treated as a separator. Whitespace is NEVER a
        separator because Upstox index keys contain spaces, e.g.
        ``NSE_INDEX|Nifty 50`` and ``NSE_INDEX|Nifty Next 50``.
        Callers that need multiple keys must use comma-separated
        values or pass a list/tuple/set.

        Whitespace around each item is trimmed; empty items are
        dropped; duplicates are removed (first occurrence wins to
        preserve caller intent).
        """
        if raw is None:
            return []

        if isinstance(raw, (list, tuple, set)):
            items: list[Any] = list(raw)
        else:
            text = str(raw).strip()
            if not text:
                return []
            # Comma-only split. Do NOT split on whitespace — that would
            # corrupt "NSE_INDEX|Nifty 50" into two invalid instrument
            # keys ("NSE_INDEX|Nifty" and "50").
            items = text.split(",")

        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            s = str(item).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    # ------------------------------------------------------------------ #
    # Subscriptions                                                      #
    # ------------------------------------------------------------------ #
    async def subscribe(
        self,
        topic: str,
        *,
        instrument_keys: list[str] | None = None,
        triples: list[tuple[str, str, str]] | None = None,
        all_instruments: bool = False,
    ) -> Subscriber:
        async with self._lock:
            sub = Subscriber(
                id=self._next_id,
                queue=asyncio.Queue(maxsize=1000),
                topic=topic,
                instrument_keys={str(k).strip() for k in (instrument_keys or [])},
                triples={
                    (
                        str(u).upper().replace(" ", ""),
                        str(s),
                        str(t).upper(),
                    )
                    for (u, s, t) in (triples or [])
                },
                all_instruments=all_instruments,
            )
            self._subs[sub.id] = sub
            self._next_id += 1
            return sub

    async def unsubscribe(self, sub_id: int) -> None:
        async with self._lock:
            self._subs.pop(sub_id, None)

    # ------------------------------------------------------------------ #
    # Publishing                                                         #
    # ------------------------------------------------------------------ #
    async def publish(
        self,
        topic: str,
        entry: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Fan out a payload to every matching subscriber (async)."""
        async with self._lock:
            subs = [s for s in self._subs.values() if s.topic == topic]

        if not subs:
            return

        message = {"instrument_key": entry["instrument_key"], **payload}
        for sub in subs:
            if not sub.matches(entry):
                continue
            try:
                sub.queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning(
                    "Subscriber %d queue full; dropping message", sub.id
                )

    def publish_sync(
        self,
        topic: str,
        entry: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Thread-safe fire-and-forget publish.

        Must only be called after ``set_loop`` has been called with the
        running loop. Safe to call from any worker thread.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.publish(topic, entry, payload),
                loop,
            )
        except Exception:
            logger.exception("publish_sync failed for topic=%s", topic)

    # ------------------------------------------------------------------ #
    # Filter helper                                                      #
    # ------------------------------------------------------------------ #
    @staticmethod
    def resolve_subscription_params(
        instrument_key: str | list[str] | None,
        underlying: str | None,
        strike: str | None,
        strike_type: str | None,
    ) -> tuple[list[str], list[tuple[str, str, str]], bool]:
        """Return ``(keys, triples, all_instruments)`` for a subscribe call.

        ``instrument_key`` may be:

          * ``None``                     -> no keys added
          * ``"NSE_FO|123"``             -> single key
          * ``"NSE_FO|123,NSE_FO|456"``  -> comma-separated list
          * ``["NSE_FO|123", ...]``      -> list

        When ``underlying`` + ``strike`` + ``strike_type`` are all
        provided, they are resolved to an additional instrument key and
        appended (or, on miss, added as a triple filter). Duplicates
        are removed while preserving caller order.
        """
        from services.instrument_registry import instrument_registry

        keys: list[str] = LiveBroadcast.parse_instrument_list(instrument_key)
        triples: list[tuple[str, str, str]] = []

        if underlying and strike and strike_type:
            entry = instrument_registry.resolve(underlying, strike, strike_type)
            if entry:
                if entry["instrument_key"] not in keys:
                    keys.append(entry["instrument_key"])
            else:
                triple = (
                    underlying.upper().replace(" ", ""),
                    str(strike),
                    strike_type.upper(),
                )
                if triple not in triples:
                    triples.append(triple)

        return keys, triples, False


live_broadcast = LiveBroadcast()