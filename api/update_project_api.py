from fastapi import APIRouter
import subprocess
import os
import platform

router = APIRouter(prefix="/api", tags=["Project Update"])


@router.get("/update-project")
async def update_project():

    # Allow only Linux/Ubuntu deployments
    if platform.system().lower() != "linux":
        return {
            "success": False,
            "message": (
                "update_project endpoint is supported only on Ubuntu/Linux deployments."
            ),
            "os": platform.system(),
        }

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        result = subprocess.run(
            ["python3", "update_project.py"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=300,
        )

        success = result.returncode == 0

        return {
            "success": success,
            "message": (
                "Project updated successfully." if success else "Project update failed."
            ),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    except Exception as ex:
        return {
            "success": False,
            "message": "Exception occurred while executing update_project.py",
            "error": str(ex),
        }
