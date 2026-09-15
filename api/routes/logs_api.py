from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

router = APIRouter(
    prefix="/api/logs",
    tags=["Logs"],
)

# Logs directory:
# project/
# └── api/
#     └── logs/
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def get_log_file(filename: str) -> Path:
    """
    Resolve a requested log filename safely.

    Prevents path traversal such as:
        ../../.env
        ../main.py
    """
    requested_file = (LOGS_DIR / filename).resolve()

    try:
        requested_file.relative_to(LOGS_DIR.resolve())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid log filename.",
        )

    return requested_file


@router.get("")
async def list_logs():
    """
    List all files in the api/logs directory.
    """

    if not LOGS_DIR.exists():
        return {
            "success": True,
            "logs": [],
            "count": 0,
        }

    if not LOGS_DIR.is_dir():
        raise HTTPException(
            status_code=500,
            detail="Logs path is not a directory.",
        )

    files = []

    for file_path in LOGS_DIR.iterdir():
        if not file_path.is_file():
            continue

        stat = file_path.stat()

        files.append(
            {
                "filename": file_path.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
        )

    # Newest files first
    files.sort(
        key=lambda x: x["modified"],
        reverse=True,
    )

    return {
        "success": True,
        "logs": files,
        "count": len(files),
    }


@router.get("/{filename}", response_class=PlainTextResponse)
async def get_log(filename: str):
    """
    Return the contents of a specific log file.
    """

    file_path = get_log_file(filename)

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Log file '{filename}' not found.",
        )

    if not file_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"'{filename}' is not a file.",
        )

    try:
        content = file_path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        return content

    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to read log file: {exc}",
        )
