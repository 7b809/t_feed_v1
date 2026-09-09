from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["Logs"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
TEMPLATES_DIR = PROJECT_ROOT / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

ALLOWED_LOG_EXTENSIONS = {".log", ".txt"}


def get_available_log_files() -> list:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    files = []

    for file_path in sorted(
        LOGS_DIR.iterdir(),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        if not file_path.is_file():
            continue

        if file_path.suffix.lower() not in ALLOWED_LOG_EXTENSIONS:
            continue

        stat = file_path.stat()

        files.append(
            {
                "filename": file_path.name,
                "size_bytes": stat.st_size,
                "modified_time": stat.st_mtime,
            }
        )

    return files


def validate_log_file(filename: str) -> Path:
    if not filename or filename in {".", ".."}:
        raise HTTPException(
            status_code=400,
            detail="Invalid log filename",
        )

    requested_path = Path(filename)

    if requested_path.name != filename:
        raise HTTPException(
            status_code=400,
            detail="Invalid log filename",
        )

    if requested_path.suffix.lower() not in ALLOWED_LOG_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported log file type",
        )

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logs_directory = LOGS_DIR.resolve()
    log_file = (LOGS_DIR / filename).resolve()

    try:
        log_file.relative_to(logs_directory)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid log file path",
        )

    if not log_file.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Log file not found: {filename}",
        )

    if not log_file.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Not a log file: {filename}",
        )

    return log_file


def read_log_file(log_file: Path) -> list:
    with log_file.open(
        mode="r",
        encoding="utf-8",
        errors="ignore",
    ) as file:
        return file.readlines()

@router.get("/logs", response_class=HTMLResponse, include_in_schema=False)
async def show_logs(request: Request):
    log_files = get_available_log_files()

    return templates.TemplateResponse(
        request=request,
        name="show_logs.html",
        context={
            "app_name": "T Feed",
            "logs": log_files,
        },
    )


@router.get(
    "/ui/logs/{filename}",
    response_class=PlainTextResponse,
    include_in_schema=False,
)
def show_log_file_content(filename: str):
    try:
        log_file = validate_log_file(filename)

        with log_file.open(
            mode="r",
            encoding="utf-8",
            errors="ignore",
        ) as file:
            return PlainTextResponse(
                content=file.read(),
                media_type="text/plain; charset=utf-8",
            )

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read log file: {exc}",
        ) from exc


@router.get("/api/logs")
def list_log_files():
    try:
        files = get_available_log_files()

        return {
            "success": True,
            "logs_directory": str(LOGS_DIR),
            "count": len(files),
            "files": files,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list log files: {exc}",
        ) from exc


@router.get("/api/logs/{filename}")
def get_log_file(
    filename: str,
    lines: int = Query(
        default=200,
        ge=1,
        le=5000,
        description="Number of latest log lines to return",
    ),
):
    try:
        log_file = validate_log_file(filename)
        all_lines = read_log_file(log_file)
        selected_lines = all_lines[-lines:]

        return {
            "success": True,
            "filename": filename,
            "path": str(log_file),
            "total_lines": len(all_lines),
            "returned_lines": len(selected_lines),
            "first_returned_line": (
                len(all_lines) - len(selected_lines) + 1
                if selected_lines
                else 0
            ),
            "logs": selected_lines,
        }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read log file: {exc}",
        ) from exc
