from fastapi import APIRouter, HTTPException, Request
from app.api.schemas import ActionResponse, HealthResponse, StatusResponse
router=APIRouter(prefix="/api")
def sup(request: Request): return request.app.state.supervisor
@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    s=sup(request); workers=list(s.workers.values())
    return {"status":"ok" if s.running else "stopped","in_market_hours":s.status()["in_market_hours"],"supervisor_running":s.running,
      "instruments":len(s.store.active_by_key()),"active_workers":sum(not t.done() for t in s.worker_tasks.values()),
      "connected_workers":sum(w.status.state=="connected" for w in workers),"total_messages_written":s.total_messages_written,"last_message_at":s.last_message_at}
@router.get("/status", response_model=StatusResponse)
async def status(request: Request): return sup(request).status()
@router.get("/subscriptions")
async def subscriptions(request: Request): return sup(request).store.snapshot or {"active":[]}
@router.post("/refresh")
async def refresh(request: Request):
    s=sup(request)
    try:
        snapshot=await s.refresh_and_reconcile() if s.status()["in_market_hours"] else await s.store.fetch()
        return {"status":"refreshed","instruments":len(snapshot.get("active",[])),"workers_reconciled":s.status()["in_market_hours"]}
    except Exception as exc: raise HTTPException(status_code=502, detail=str(exc)) from exc
@router.post("/start", response_model=ActionResponse)
async def start(request: Request):
    changed=await sup(request).start(); return {"status":"started" if changed else "already_running","changed":changed}
@router.post("/stop", response_model=ActionResponse)
async def stop(request: Request):
    s=sup(request); changed=s.running; await s.stop(); return {"status":"stopped","changed":changed}
