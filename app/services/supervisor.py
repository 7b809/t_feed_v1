import asyncio, logging
from typing import Any
from app.core.config import Settings
from app.core.market_hours import is_market_hours, local_now, next_open, seconds_until_close
from app.services.subscriptions import SubscriptionStore
from app.services.ws_worker import WebSocketWorker

logger=logging.getLogger(__name__)
class Supervisor:
    def __init__(self, settings: Settings):
        self.settings=settings; self.store=SubscriptionStore(settings); self.stop_event=asyncio.Event()
        self.task: asyncio.Task|None=None; self.workers: dict[tuple[str,str], WebSocketWorker]={}; self.worker_tasks: dict[tuple[str,str],asyncio.Task]={}
        self.next_wakeup_at: str|None=None; self.total_messages_written=0; self.last_message_at: str|None=None
        self._lock=asyncio.Lock(); self.semaphore=asyncio.Semaphore(settings.ws_max_concurrent_connections) if settings.ws_max_concurrent_connections else None

    @property
    def running(self): return bool(self.task and not self.task.done())
    async def start(self):
        async with self._lock:
            if self.running: return False
            self.stop_event=asyncio.Event(); self.task=asyncio.create_task(self.run(), name="feed-supervisor"); return True
    async def stop(self):
        async with self._lock:
            self.stop_event.set(); task=self.task; self.task=None
        await self._cancel_all()
        if task and not task.done():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
        return True

    def _message(self, timestamp: str): self.total_messages_written+=1; self.last_message_at=timestamp
    async def interruptible_sleep(self, seconds: float):
        try: await asyncio.wait_for(self.stop_event.wait(), timeout=max(0,seconds))
        except asyncio.TimeoutError: pass

    async def refresh_and_reconcile(self):
        snapshot=await self.store.fetch(); await self.reconcile(self.store.active_by_key()); return snapshot
    async def reconcile(self, active: dict[str,dict[str,Any]]):
        desired={(key,feed) for key in active for feed in ("ema","raw")}
        for token in list(self.worker_tasks):
            if token not in desired:
                task=self.worker_tasks.pop(token); task.cancel(); self.workers.pop(token,None)
                try: await task
                except asyncio.CancelledError: pass
        for key,item in active.items():
            for feed,url_key in (("ema","ema_connect_url"),("raw","raw_connect_url")):
                token=(key,feed)
                if token not in self.worker_tasks or self.worker_tasks[token].done():
                    worker=WebSocketWorker(self.settings,key,feed,item[url_key],self.semaphore,self._message)
                    self.workers[token]=worker; self.worker_tasks[token]=asyncio.create_task(worker.run(),name=f"ws:{feed}:{key}")
    async def _cancel_all(self):
        tasks=list(self.worker_tasks.values())
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        self.worker_tasks.clear()

    async def run(self):
        logger.info("Supervisor started")
        try:
            while not self.stop_event.is_set():
                if not is_market_hours(self.settings):
                    await self._cancel_all(); wake=next_open(self.settings); self.next_wakeup_at=wake.isoformat()
                    logger.info("Outside market hours; next wake-up %s", self.next_wakeup_at)
                    await self.interruptible_sleep((wake-local_now(self.settings)).total_seconds()); continue
                self.next_wakeup_at=None
                try: await self.refresh_and_reconcile()
                except Exception: logger.exception("Subscription refresh failed; keeping existing workers")
                sleep_for=min(self.settings.subscriptions_refresh_seconds, max(1,seconds_until_close(self.settings)))
                await self.interruptible_sleep(sleep_for)
            await self._cancel_all()
        except asyncio.CancelledError:
            await self._cancel_all(); raise
        finally: logger.info("Supervisor stopped")

    def status(self):
        return {"in_market_hours":is_market_hours(self.settings),"next_wakeup_at":self.next_wakeup_at,
          "instruments":len(self.store.active_by_key()),"workers":[w.status.public() for w in self.workers.values()]}
