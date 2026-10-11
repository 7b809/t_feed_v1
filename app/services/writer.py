import asyncio, json, os, re
from datetime import datetime
from pathlib import Path
from typing import Any
import aiofiles
from app.core.config import Settings
from app.core.market_hours import market_tz

def sanitize_key(value: str) -> str:
    value=value.replace("|", "__").replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)

class JsonlWriter:
    def __init__(self, settings: Settings, feed: str, instrument_key: str, url: str):
        self.settings=settings; self.feed=feed; self.instrument_key=instrument_key; self.url=url
        self._file=None; self._date=None; self._path: Path | None=None
        self._since_flush=0; self._last_flush=0.0; self._lock=asyncio.Lock()

    @property
    def path(self) -> str | None: return str(self._path) if self._path else None

    async def _ensure_open(self, now: datetime) -> None:
        date_text=now.astimezone(market_tz(self.settings)).date().isoformat()
        if self._file and self._date == date_text: return
        await self.close()
        folder=self.settings.files_dir/self.feed; folder.mkdir(parents=True, exist_ok=True)
        self._path=folder/f"{sanitize_key(self.instrument_key)}__{date_text}.jsonl"
        self._file=await aiofiles.open(self._path, mode="a", encoding="utf-8")
        self._date=date_text; self._last_flush=asyncio.get_running_loop().time(); self._since_flush=0

    async def write(self, raw: str | bytes) -> str:
        async with self._lock:
            now=datetime.now(market_tz(self.settings)); await self._ensure_open(now)
            raw_text=raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            record: dict[str, Any]={"received_at":now.isoformat(),"feed":self.feed,"instrument_key":self.instrument_key,"url":self.url,"raw":raw_text}
            try: record["parsed"]=json.loads(raw_text)
            except (json.JSONDecodeError, TypeError): pass
            await self._file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._since_flush+=1; loop_now=asyncio.get_running_loop().time()
            if self._since_flush >= self.settings.file_flush_every_n_messages or loop_now-self._last_flush >= self.settings.file_flush_interval_seconds:
                await self.flush()
            return now.isoformat()

    async def flush(self) -> None:
        if not self._file: return
        await self._file.flush()
        fileno=self._file.fileno(); await asyncio.to_thread(os.fsync, fileno)
        self._since_flush=0; self._last_flush=asyncio.get_running_loop().time()

    async def close(self) -> None:
        if self._file:
            await self.flush(); await self._file.close(); self._file=None
