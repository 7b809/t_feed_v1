import asyncio
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.option_service import options_cache
from services.upstox_websocket import upstox_streamer


logger = get_logger(__file__)


@dataclass
class ServiceActionResult:
    success: bool
    action: str
    message: str
    timestamp: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ServiceControlService:
    """
    Controls internal application components and optionally requests
    operating-system service actions through an allow-listed manager.

    This class must never accept arbitrary commands from API callers.
    """

    def __init__(self):
        self._action_lock = threading.Lock()
        self._last_action: dict[str, Any] | None = None

    def _now(self) -> str:
        try:
            timezone = ZoneInfo(config.MARKET_TIMEZONE)
        except Exception:
            timezone = ZoneInfo("Asia/Kolkata")

        return datetime.now(timezone).isoformat()

    def _save_result(
        self,
        result: ServiceActionResult,
    ) -> ServiceActionResult:
        self._last_action = result.to_dict()
        return result

    def get_status(self) -> dict[str, Any]:
        loop = getattr(upstox_streamer, "loop", None)

        return {
            "success": True,
            "action": "status",
            "timestamp": self._now(),
            "process": {
                "pid": os.getpid(),
                "python": sys.executable,
            },
            "configuration": {
                "control_enabled": bool(
                    config.SERVICE_CONTROL_ENABLED
                ),
                "manager": config.SERVICE_MANAGER,
                "systemd_service_name": (
                    config.SYSTEMD_SERVICE_NAME
                    if config.SERVICE_MANAGER == "systemd"
                    else None
                ),
                "redeploy_configured": bool(
                    config.REDEPLOY_COMMAND
                ),
            },
            "application": {
                "streamer_loop_available": bool(loop),
                "streamer_loop_running": bool(
                    loop and loop.is_running()
                ),
                "subscribed_instruments": len(
                    options_cache.get("subscribed_keys", []) or []
                ),
            },
            "last_action": self._last_action,
        }

    def _run_command(
        self,
        action: str,
        command: list[str],
    ) -> ServiceActionResult:
        logger.warning(
            "Executing service-control action. action=%s command=%s",
            action,
            command,
        )

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=(
                    config.SERVICE_COMMAND_TIMEOUT_SECONDS
                ),
                check=False,
            )

            success = completed.returncode == 0

            result = ServiceActionResult(
                success=success,
                action=action,
                message=(
                    f"Action '{action}' completed successfully."
                    if success
                    else f"Action '{action}' failed."
                ),
                timestamp=self._now(),
                details={
                    "return_code": completed.returncode,
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                },
            )

            if success:
                logger.info(
                    "Service-control action completed. action=%s",
                    action,
                )
            else:
                logger.error(
                    "Service-control action failed. "
                    "action=%s return_code=%s stderr=%s",
                    action,
                    completed.returncode,
                    completed.stderr,
                )

            return self._save_result(result)

        except subprocess.TimeoutExpired as ex:
            logger.exception(
                "Service-control action timed out. action=%s",
                action,
            )

            return self._save_result(
                ServiceActionResult(
                    success=False,
                    action=action,
                    message=(
                        f"Action '{action}' exceeded the "
                        "configured timeout."
                    ),
                    timestamp=self._now(),
                    details={
                        "error": f"{type(ex).__name__}: {ex}",
                    },
                )
            )

        except Exception as ex:
            logger.exception(
                "Service-control action failed. action=%s",
                action,
            )

            return self._save_result(
                ServiceActionResult(
                    success=False,
                    action=action,
                    message=f"Action '{action}' failed.",
                    timestamp=self._now(),
                    details={
                        "error": f"{type(ex).__name__}: {ex}",
                    },
                )
            )

    def _systemd_command(
        self,
        action: str,
    ) -> list:
        allowed_actions = {
            "start",
            "stop",
            "restart",
            "status",
        }

        if action not in allowed_actions:
            raise ValueError(
                f"Unsupported systemd action: {action}"
            )

        service_name = config.SYSTEMD_SERVICE_NAME

        if not service_name:
            raise RuntimeError(
                "SYSTEMD_SERVICE_NAME is not configured."
            )

        return [
            "sudo",
            "-n",
            "systemctl",
            action,
            service_name,
        ]

    def get_external_status(self) -> ServiceActionResult:
        if config.SERVICE_MANAGER != "systemd":
            return self._save_result(
                ServiceActionResult(
                    success=True,
                    action="status",
                    message="Internal application status returned.",
                    timestamp=self._now(),
                    details=self.get_status(),
                )
            )

        return self._run_command(
            action="status",
            command=self._systemd_command("status"),
        )

    def start_service(self) -> ServiceActionResult:
        if config.SERVICE_MANAGER != "systemd":
            return self._save_result(
                ServiceActionResult(
                    success=False,
                    action="start",
                    message=(
                        "Operating-system start requires "
                        "SERVICE_MANAGER=systemd."
                    ),
                    timestamp=self._now(),
                )
            )

        return self._run_command(
            action="start",
            command=self._systemd_command("start"),
        )

    def stop_service(self) -> ServiceActionResult:
        if config.SERVICE_MANAGER != "systemd":
            return self._save_result(
                ServiceActionResult(
                    success=False,
                    action="stop",
                    message=(
                        "Operating-system stop requires "
                        "SERVICE_MANAGER=systemd."
                    ),
                    timestamp=self._now(),
                )
            )

        return self._run_command(
            action="stop",
            command=self._systemd_command("stop"),
        )

    def restart_service(self) -> ServiceActionResult:
        if config.SERVICE_MANAGER != "systemd":
            return self._save_result(
                ServiceActionResult(
                    success=False,
                    action="restart",
                    message=(
                        "Operating-system restart requires "
                        "SERVICE_MANAGER=systemd."
                    ),
                    timestamp=self._now(),
                )
            )

        return self._run_command(
            action="restart",
            command=self._systemd_command("restart"),
        )

    def redeploy_service(self) -> ServiceActionResult:
        configured_command = config.REDEPLOY_COMMAND

        if not configured_command:
            return self._save_result(
                ServiceActionResult(
                    success=False,
                    action="redeploy",
                    message="REDEPLOY_COMMAND is not configured.",
                    timestamp=self._now(),
                )
            )

        command = shlex.split(configured_command)

        if not command:
            return self._save_result(
                ServiceActionResult(
                    success=False,
                    action="redeploy",
                    message="REDEPLOY_COMMAND is empty.",
                    timestamp=self._now(),
                )
            )

        return self._run_command(
            action="redeploy",
            command=command,
        )

    def restart_streamer(self) -> ServiceActionResult:
        loop = getattr(upstox_streamer, "loop", None)

        if not loop or not loop.is_running():
            return self._save_result(
                ServiceActionResult(
                    success=False,
                    action="restart_streamer",
                    message=(
                        "Upstox streamer event loop is unavailable."
                    ),
                    timestamp=self._now(),
                )
            )

        try:
            future = asyncio.run_coroutine_threadsafe(
                upstox_streamer.restart(),
                loop,
            )

            future.result(
                timeout=(
                    config.SERVICE_COMMAND_TIMEOUT_SECONDS
                )
            )

            return self._save_result(
                ServiceActionResult(
                    success=True,
                    action="restart_streamer",
                    message="Upstox streamer restarted.",
                    timestamp=self._now(),
                    details={
                        "subscribed_instruments": len(
                            options_cache.get(
                                "subscribed_keys",
                                [],
                            )
                            or []
                        ),
                    },
                )
            )

        except Exception as ex:
            logger.exception(
                "Upstox streamer restart failed."
            )

            return self._save_result(
                ServiceActionResult(
                    success=False,
                    action="restart_streamer",
                    message="Upstox streamer restart failed.",
                    timestamp=self._now(),
                    details={
                        "error": f"{type(ex).__name__}: {ex}",
                    },
                )
            )


service_control_service = ServiceControlService()