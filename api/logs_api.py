"""Read-only application log viewer for diagnostics."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["Logs"])
PROJECT_ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory="templates")


def _log_directory(source: str) -> Path:
    if source not in {"logs", "logs_text"}:
        raise HTTPException(status_code=400, detail="source must be logs or logs_text")
    return (PROJECT_ROOT / source).resolve()


@router.get("/logs")
def logs_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="show_logs.html",
        context={"request": request, "page_title": "Application Logs"},
    )


@router.get("/api/logs")
def list_log_files(source: str = Query("logs")):
    directory = _log_directory(source)
    directory.mkdir(parents=True, exist_ok=True)
    files = [
        {"name": path.name, "size": path.stat().st_size, "modified": path.stat().st_mtime}
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".log", ".txt"}
    ]
    files.sort(key=lambda item: item["modified"], reverse=True)
    return {"source": source, "files": files}


@router.get("/api/logs/{filename}")
def read_log_file(
    filename: str,
    source: str = Query("logs"),
    lines: int = Query(500, ge=1, le=5000),
):
    directory = _log_directory(source)
    if Path(filename).name != filename or Path(filename).suffix.lower() not in {".log", ".txt"}:
        raise HTTPException(status_code=400, detail="Invalid log filename")
    target = (directory / filename).resolve()
    try:
        target.relative_to(directory)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid log filename") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    try:
        content = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Unable to read log file") from exc
    return {"name": filename, "source": source, "lines": content[-lines:]}
