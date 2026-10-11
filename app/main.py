from fastapi import FastAPI
from fastapi.responses import FileResponse
from app.api.routes import router
from app.core.lifespan import lifespan
app=FastAPI(title="Dual WebSocket Feed Capture",version="1.0.0",lifespan=lifespan)
app.include_router(router)
@app.get("/",include_in_schema=False)
async def index(): return FileResponse("app/static/index.html")
