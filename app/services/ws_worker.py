import asyncio, logging
from dataclasses import dataclass, asdict
from typing import Any, Callable
import websockets
from app.core.config import Settings
from app.core.market_hours import is_market_hours, local_now, seconds_until_close
from app.services.writer import JsonlWriter

logger=logging.getLogger(__name__)

@dataclass
class WorkerStatus:
    instrument_key: str; feed: str; url: str
    state: str="pending"; connected_at: str|None=None; last_message_at: str|None=None
    messages: int=0; reconnects: int=0; last_error: str|None=None; file: str|None=None
    def public(self): return asdict(self)

class WebSocketWorker:
    def __init__(self, settings: Settings, instrument_key: str, feed: str, url: str,
                 semaphore: asyncio.Semaphore | None, on_message: Callable[[str], None]):
        self.settings=settings; self.status=WorkerStatus(instrument_key, feed, url)
        self.semaphore=semaphore; self.on_message=on_message
        self.writer=JsonlWriter(settings, feed, instrument_key, url); self._socket=None

    def transition(self, state: str, error: str|None=None):
        self.status.state=state
        if error is not None: self.status.last_error=error
        logger.info("worker key=%s feed=%s state=%s", self.status.instrument_key, self.status.feed, state)

    async def run(self):
        delay=self.settings.ws_reconnect_initial_seconds
        try:
            while is_market_hours(self.settings):
                received=False; self.transition("connecting")
                try:
                    cm=self.semaphore if self.semaphore else _NullAsyncContext()
                    async with cm:
                        if not is_market_hours(self.settings): break
                        async with websockets.connect(self.status.url, ping_interval=self.settings.ws_ping_interval_seconds,
                            ping_timeout=self.settings.ws_ping_timeout_seconds, open_timeout=self.settings.ws_open_timeout_seconds,
                            close_timeout=2) as socket:
                            self._socket=socket; self.status.connected_at=local_now(self.settings).isoformat(); self.status.last_error=None
                            self.transition("connected")
                            while is_market_hours(self.settings):
                                timeout=min(1.0, max(.1, seconds_until_close(self.settings)))
                                try: message=await asyncio.wait_for(socket.recv(), timeout=timeout)
                                except asyncio.TimeoutError: continue
                                except websockets.ConnectionClosed: break
                                timestamp=await self.writer.write(message); received=True
                                self.status.messages+=1; self.status.last_message_at=timestamp; self.status.file=self.writer.path
                                self.on_message(timestamp)
                    self._socket=None
                    if not is_market_hours(self.settings): break
                    raise ConnectionError("WebSocket closed")
                except asyncio.CancelledError: raise
                except Exception as exc:
                    self.status.last_error=str(exc); self.status.reconnects+=1
                    self.transition("reconnecting", str(exc)); logger.warning("Reconnect key=%s feed=%s in %.1fs: %s", self.status.instrument_key, self.status.feed, delay, exc)
                    logger.exception("Worker socket exception")
                    if received: delay=self.settings.ws_reconnect_initial_seconds
                    if not is_market_hours(self.settings): break
                    try: await asyncio.sleep(delay)
                    except asyncio.CancelledError: raise
                    delay=min(delay*self.settings.ws_reconnect_multiplier, self.settings.ws_reconnect_max_seconds)
            self.transition("closed")
        except asyncio.CancelledError:
            self.transition("stopped"); raise
        finally:
            if self._socket:
                try: await self._socket.close(code=1001, reason="service stopping")
                except Exception: pass
            await self.writer.close(); self.status.file=self.writer.path

class _NullAsyncContext:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
