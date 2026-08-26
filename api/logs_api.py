import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(
    prefix="/api/logs",
    tags=["Logs"],
)

# Project root:
# D:\files\temp_ticks\t_feed_v1-v2-dev
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Logs directory:
# D:\files\temp_ticks\t_feed_v1-v2-dev\logs
LOGS_DIR = PROJECT_ROOT / "logs"


# ============================================================
# GET AVAILABLE LOG FILES
# ============================================================
#
# Example:
# GET /api/logs
#
# Returns all files currently available inside the logs folder.
#
# ============================================================


@router.get("")
def list_log_files():
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        files = []

        for file_path in sorted(LOGS_DIR.iterdir()):
            if file_path.is_file():
                stat = file_path.stat()

                files.append(
                    {
                        "filename": file_path.name,
                        "size_bytes": stat.st_size,
                        "modified_time": stat.st_mtime,
                    }
                )

        return {
            "success": True,
            "logs_directory": str(LOGS_DIR),
            "count": len(files),
            "files": files,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list log files: {str(e)}",
        )


# ============================================================
# GET SELECTED LOG FILE
# ============================================================
#
# Example:
#
# GET /api/logs/telegram_service.log
#
# Optional:
#
# GET /api/logs/telegram_service.log?lines=200
#
# By default, the latest 200 lines are returned.
#
# ============================================================


@router.get("/{filename}")
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
        # ----------------------------------------------------
        # Security:
        # Only allow a filename, not a path.
        # ----------------------------------------------------
        requested_path = Path(filename)

        if requested_path.name != filename:
            raise HTTPException(
                status_code=400,
                detail="Invalid log filename",
            )

        log_file = LOGS_DIR / filename

        # ----------------------------------------------------
        # Make sure the resolved file remains inside logs/
        # ----------------------------------------------------
        try:
            log_file.resolve().relative_to(LOGS_DIR.resolve())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid log file path",
            )

        # ----------------------------------------------------
        # Check file exists
        # ----------------------------------------------------
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

        # ----------------------------------------------------
        # Read log
        # ----------------------------------------------------
        with open(
            log_file,
            "r",
            encoding="utf-8",
            errors="ignore",
        ) as f:
            all_lines = f.readlines()

        # Latest N lines
        selected_lines = all_lines[-lines:]

        return {
            "success": True,
            "filename": filename,
            "path": str(log_file),
            "total_lines": len(all_lines),
            "returned_lines": len(selected_lines),
            "logs": selected_lines,
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read log file: {str(e)}",
        )
