import asyncio, logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
import httpx
from app.core.config import Settings

logger=logging.getLogger(__name__)

def derive_raw_base(snapshot: dict[str, Any], fallback: str) -> str:
    base=snapshot.get("ws_base_url")
    if not base: return fallback.rstrip("?")
    parts=urlsplit(str(base))
    if parts.path and parts.path != "/": return str(base).rstrip("?")
    return urlunsplit((parts.scheme, parts.netloc, "/ws/market", "", ""))

def raw_url(base: str, instrument_key: str) -> str:
    separator="&" if "?" in base else "?"
    return f"{base}{separator}instrument_key={quote(instrument_key, safe='')}"

@dataclass
class SubscriptionStore:
    settings: Settings
    snapshot: dict[str, Any] | None = None
    last_error: str | None = None

    async def fetch(self, attempts: int = 3) -> dict[str, Any]:
        delay=1.0
        for attempt in range(1, attempts+1):
            try:
                async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                    response=await client.get(self.settings.subscriptions_api_url)
                    response.raise_for_status(); payload=response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("active"), list):
                    raise ValueError("Subscription API must return an object with an active list")
                base=derive_raw_base(payload, self.settings.raw_feed_base)
                active=[]
                for item in payload["active"]:
                    if not isinstance(item, dict) or not item.get("instrument_key") or not item.get("connect_url"):
                        logger.warning("Skipping invalid subscription item: %r", item); continue
                    enriched=dict(item); enriched["ema_connect_url"]=item["connect_url"]
                    enriched["raw_connect_url"]=raw_url(base, item["instrument_key"]); active.append(enriched)
                self.snapshot={**payload, "active": active, "raw_feed_base": base}; self.last_error=None
                logger.info("Fetched %d active subscriptions", len(active)); return self.snapshot
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.last_error=str(exc); logger.warning("Subscription fetch %d/%d failed: %s", attempt, attempts, exc)
                if attempt < attempts: await asyncio.sleep(delay); delay*=2
        raise RuntimeError(f"Unable to fetch subscriptions: {self.last_error}")

    def active_by_key(self) -> dict[str, dict[str, Any]]:
        return {x["instrument_key"]: x for x in (self.snapshot or {}).get("active", [])}
