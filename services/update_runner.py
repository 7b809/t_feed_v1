
import asyncio
import os
import uuid

from datetime import datetime, timezone
from pathlib import Path

from core.config import Settings
from core.logger import get_logger
from models.schemas import JobStatus
from services.notifier import TelegramNotifier


logger = get_logger("update_runner")


# ============================================================
# UTC Time
# ============================================================

def utcnow():
    return datetime.now(timezone.utc)


# ============================================================
# Update Manager
# ============================================================

class UpdateManager:

    def __init__(
        self,
        settings: Settings,
        notifier: TelegramNotifier,
    ):
        self.settings = settings
        self.notifier = notifier

        # ----------------------------------------------------
        # Job storage
        # ----------------------------------------------------

        self.jobs: dict[str, JobStatus] = {}

        # ----------------------------------------------------
        # One job at a time per project
        # ----------------------------------------------------

        self.locks = {
            name: asyncio.Lock()
            for name in settings.projects
        }

        # ----------------------------------------------------
        # Background tasks
        # ----------------------------------------------------

        self.tasks: set[asyncio.Task] = set()

        logger.info(
            "UpdateManager initialized | projects=%s",
            list(settings.projects.keys()),
        )

        logger.info(
            "Project-control working directory | cwd=%s",
            Path.cwd(),
        )

        for project_name, project_config in settings.projects.items():
            logger.info(
                "Configured project | name=%s | folder=%s | command=%s",
                project_name,
                project_config["folder"],
                project_config["command"],
            )

    # ========================================================
    # Submit Job
    # ========================================================

    def submit(
        self,
        project: str,
        requested_by: str,
        chat_id: int | None = None,
    ) -> JobStatus:

        logger.info(
            "============================================================"
        )

        logger.info(
            "UPDATE REQUEST RECEIVED | project=%s | requested_by=%s | chat_id=%s",
            project,
            requested_by,
            chat_id,
        )

        logger.info(
            "============================================================"
        )

        # ----------------------------------------------------
        # Validate project
        # ----------------------------------------------------

        if project not in self.settings.projects:

            logger.warning(
                "Job submission rejected | unknown project=%s | requested_by=%s",
                project,
                requested_by,
            )

            raise KeyError(project)

        # ----------------------------------------------------
        # Show selected project configuration
        # ----------------------------------------------------

        project_config = self.settings.projects[project]

        logger.info(
            "Selected project confirmed | project=%s",
            project,
        )

        logger.info(
            "Selected project folder | project=%s | folder=%s",
            project,
            project_config["folder"],
        )

        logger.info(
            "Selected project command | project=%s | command=%s",
            project,
            project_config["command"],
        )

        # ----------------------------------------------------
        # Create job
        # ----------------------------------------------------

        job = JobStatus(
            job_id=uuid.uuid4().hex,
            project=project,
            status="queued",
            requested_by=requested_by,
            created_at=utcnow(),
        )

        self.jobs[job.job_id] = job

        logger.info(
            "Job created | job_id=%s | project=%s | status=queued | requested_by=%s",
            job.job_id,
            project,
            requested_by,
        )

        # ----------------------------------------------------
        # Create background task
        # ----------------------------------------------------

        task = asyncio.create_task(
            self._run(
                job,
                chat_id,
            )
        )

        self.tasks.add(task)

        task.add_done_callback(
            self.tasks.discard
        )

        logger.info(
            "Background task created | job_id=%s | project=%s | active_tasks=%d",
            job.job_id,
            project,
            len(self.tasks),
        )

        return job

    # ========================================================
    # Get Job
    # ========================================================

    def get(
        self,
        job_id: str,
    ) -> JobStatus | None:

        job = self.jobs.get(job_id)

        if job is None:

            logger.warning(
                "Job lookup failed | job_id=%s | result=not_found",
                job_id,
            )

            return None

        logger.info(
            "Job lookup | job_id=%s | project=%s | status=%s",
            job.job_id,
            job.project,
            job.status,
        )

        return job

    # ========================================================
    # Run Job
    # ========================================================

    async def _run(
        self,
        job: JobStatus,
        chat_id: int | None,
    ):

        logger.info(
            "============================================================"
        )

        logger.info(
            "PROJECT UPDATE STARTING | job_id=%s | project=%s",
            job.job_id,
            job.project,
        )

        logger.info(
            "Project-control process directory | cwd=%s",
            Path.cwd(),
        )

        logger.info(
            "Requested by | %s",
            job.requested_by,
        )

        logger.info(
            "Telegram chat ID | %s",
            chat_id,
        )

        logger.info(
            "============================================================"
        )

        # ----------------------------------------------------
        # Queue notification
        # ----------------------------------------------------

        await self.notifier.send(
            (
                f"QUEUE | {job.project}\n"
                f"Job: {job.job_id}\n"
                f"Requested by: {job.requested_by}"
            ),
            chat_id,
        )

        logger.info(
            "QUEUE notification sent | job_id=%s | project=%s",
            job.job_id,
            job.project,
        )

        # ----------------------------------------------------
        # Get project configuration
        # ----------------------------------------------------

        project_config = self.settings.projects[
            job.project
        ]

        project_dir = Path(
            project_config["folder"]
        )

        command = list(
            project_config["command"]
        )

        # ----------------------------------------------------
        # Display exact execution information
        # ----------------------------------------------------

        logger.info(
            "------------------------------------------------------------"
        )

        logger.info(
            "PROJECT EXECUTION CONFIGURATION"
        )

        logger.info(
            "Project name : %s",
            job.project,
        )

        logger.info(
            "Project folder : %s",
            project_dir,
        )

        logger.info(
            "Command list : %s",
            command,
        )

        logger.info(
            "Working directory (cwd) : %s",
            project_dir,
        )

        logger.info(
            "Equivalent shell command : cd %s && %s",
            project_dir,
            " ".join(command),
        )

        logger.info(
            "------------------------------------------------------------"
        )

        # ----------------------------------------------------
        # Lock project
        # ----------------------------------------------------

        logger.info(
            "Waiting for project lock | job_id=%s | project=%s",
            job.job_id,
            job.project,
        )

        async with self.locks[job.project]:

            logger.info(
                "Project lock acquired | job_id=%s | project=%s",
                job.job_id,
                job.project,
            )

            # ------------------------------------------------
            # Validate project directory
            # ------------------------------------------------

            logger.info(
                "Checking project directory | path=%s",
                project_dir,
            )

            if not project_dir.is_dir():

                error = (
                    f"Project directory does not exist: "
                    f"{project_dir}"
                )

                job.status = "failed"
                job.finished_at = utcnow()
                job.error = error

                logger.error(
                    "Project directory validation FAILED | "
                    "job_id=%s | project=%s | path=%s",
                    job.job_id,
                    job.project,
                    project_dir,
                )

                await self.notifier.send(
                    (
                        f"FAILED | {job.project}\n"
                        f"Job: {job.job_id}\n"
                        f"{error}"
                    ),
                    chat_id,
                )

                return

            logger.info(
                "Project directory validation PASSED | "
                "project=%s | path=%s",
                job.project,
                project_dir,
            )

            # ------------------------------------------------
            # Validate command
            # ------------------------------------------------

            if not command:

                error = (
                    f"No execution command configured "
                    f"for project: {job.project}"
                )

                job.status = "failed"
                job.finished_at = utcnow()
                job.error = error

                logger.error(
                    "Command validation FAILED | "
                    "job_id=%s | project=%s | error=%s",
                    job.job_id,
                    job.project,
                    error,
                )

                await self.notifier.send(
                    (
                        f"FAILED | {job.project}\n"
                        f"Job: {job.job_id}\n"
                        f"{error}"
                    ),
                    chat_id,
                )

                return

            logger.info(
                "Command validation PASSED | project=%s | command=%s",
                job.project,
                command,
            )

            # ------------------------------------------------
            # Validate executable/script when possible
            # ------------------------------------------------

            executable = command[0]

            logger.info(
                "Command executable | project=%s | executable=%s",
                job.project,
                executable,
            )

            if len(command) >= 2:

                possible_script = project_dir / command[1]

                if executable in (
                    "python",
                    "python3",
                    "python3.10",
                    "bash",
                    "sh",
                ):

                    logger.info(
                        "Checking required script | "
                        "project=%s | script=%s",
                        job.project,
                        possible_script,
                    )

                    if not possible_script.is_file():

                        error = (
                            f"Required script not found: "
                            f"{possible_script}"
                        )

                        job.status = "failed"
                        job.finished_at = utcnow()
                        job.error = error

                        logger.error(
                            "Required script validation FAILED | "
                            "job_id=%s | project=%s | script=%s",
                            job.job_id,
                            job.project,
                            possible_script,
                        )

                        await self.notifier.send(
                            (
                                f"FAILED | {job.project}\n"
                                f"Job: {job.job_id}\n"
                                f"{error}"
                            ),
                            chat_id,
                        )

                        return

                    logger.info(
                        "Required script validation PASSED | "
                        "project=%s | script=%s",
                        job.project,
                        possible_script,
                    )

            # ------------------------------------------------
            # Mark job as running
            # ------------------------------------------------

            job.status = "running"
            job.started_at = utcnow()

            logger.info(
                "============================================================"
            )

            logger.info(
                "EXECUTING PROJECT UPDATE"
            )

            logger.info(
                "Project : %s",
                job.project,
            )

            logger.info(
                "Directory : %s",
                project_dir,
            )

            logger.info(
                "Command : %s",
                " ".join(command),
            )

            logger.info(
                "Full execution : cd %s && %s",
                project_dir,
                " ".join(command),
            )

            logger.info(
                "============================================================"
            )

            # ------------------------------------------------
            # Start notification
            # ------------------------------------------------

            await self.notifier.send(
                (
                    f"START | {job.project}\n"
                    f"Command: {' '.join(command)}\n"
                    f"Directory: {project_dir}"
                ),
                chat_id,
            )

            logger.info(
                "START notification sent | job_id=%s | project=%s",
                job.job_id,
                job.project,
            )

            process = None
            output = bytearray()

            try:

                # ============================================
                # Environment
                # ============================================

                env = os.environ.copy()

                env["PYTHONUNBUFFERED"] = "1"

                logger.info(
                    "Subprocess environment prepared | "
                    "job_id=%s | PYTHONUNBUFFERED=1",
                    job.job_id,
                )

                # ============================================
                # Start process
                # ============================================

                logger.info(
                    "------------------------------------------------------------"
                )

                logger.info(
                    "STARTING SUBPROCESS"
                )

                logger.info(
                    "Executable : %s",
                    executable,
                )

                logger.info(
                    "Arguments : %s",
                    command[1:],
                )

                logger.info(
                    "cwd : %s",
                    project_dir,
                )

                logger.info(
                    "Command : %s",
                    " ".join(command),
                )

                logger.info(
                    "------------------------------------------------------------"
                )

                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=project_dir,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )

                logger.info(
                    "SUBPROCESS STARTED SUCCESSFULLY | "
                    "job_id=%s | project=%s | pid=%s",
                    job.job_id,
                    job.project,
                    process.pid,
                )

                logger.info(
                    "Subprocess working directory | "
                    "pid=%s | cwd=%s",
                    process.pid,
                    project_dir,
                )

                logger.info(
                    "Subprocess command | pid=%s | command=%s",
                    process.pid,
                    " ".join(command),
                )

                # ============================================
                # Read process output
                # ============================================

                async def read_output():

                    assert process.stdout

                    while True:

                        chunk = await process.stdout.read(
                            4096
                        )

                        if not chunk:
                            break

                        output.extend(chunk)

                        text = chunk.decode(
                            errors="replace"
                        ).rstrip()

                        if text:

                            logger.info(
                                "%s | PROJECT OUTPUT | %s",
                                job.job_id,
                                text,
                            )

                # ============================================
                # Wait for process + output
                # ============================================

                logger.info(
                    "Waiting for project command to finish | "
                    "job_id=%s | pid=%s | timeout=%s seconds",
                    job.job_id,
                    process.pid,
                    self.settings.command_timeout_seconds,
                )

                await asyncio.wait_for(
                    asyncio.gather(
                        process.wait(),
                        read_output(),
                    ),
                    timeout=self.settings.command_timeout_seconds,
                )

                # ============================================
                # Process completed
                # ============================================

                text = output.decode(
                    errors="replace"
                )

                job.output_tail = text[
                    -self.settings.telegram_output_max_chars:
                ]

                job.return_code = process.returncode
                job.finished_at = utcnow()

                logger.info(
                    "------------------------------------------------------------"
                )

                logger.info(
                    "PROJECT COMMAND COMPLETED"
                )

                logger.info(
                    "Project : %s",
                    job.project,
                )

                logger.info(
                    "Job ID : %s",
                    job.job_id,
                )

                logger.info(
                    "PID : %s",
                    process.pid,
                )

                logger.info(
                    "Directory : %s",
                    project_dir,
                )

                logger.info(
                    "Command : %s",
                    " ".join(command),
                )

                logger.info(
                    "Exit code : %s",
                    process.returncode,
                )

                logger.info(
                    "------------------------------------------------------------"
                )

                # ============================================
                # Success
                # ============================================

                if process.returncode == 0:

                    job.status = "succeeded"

                    logger.info(
                        "============================================================"
                    )

                    logger.info(
                        "PROJECT UPDATE SUCCESS"
                    )

                    logger.info(
                        "Project : %s",
                        job.project,
                    )

                    logger.info(
                        "Job ID : %s",
                        job.job_id,
                    )

                    logger.info(
                        "Directory : %s",
                        project_dir,
                    )

                    logger.info(
                        "Command : %s",
                        " ".join(command),
                    )

                    logger.info(
                        "Exit code : 0"
                    )

                    logger.info(
                        "============================================================"
                    )

                    await self.notifier.send(
                        (
                            f"SUCCESS | {job.project}\n"
                            f"Job: {job.job_id}\n"
                            f"Exit code: 0\n\n"
                            f"Output tail:\n"
                            f"{job.output_tail or '(no output)'}"
                        ),
                        chat_id,
                    )

                    logger.info(
                        "SUCCESS notification sent | "
                        "job_id=%s | project=%s",
                        job.job_id,
                        job.project,
                    )

                # ============================================
                # Command failed
                # ============================================

                else:

                    job.status = "failed"

                    job.error = (
                        "Command exited with code "
                        f"{process.returncode}"
                    )

                    logger.error(
                        "============================================================"
                    )

                    logger.error(
                        "PROJECT UPDATE FAILED"
                    )

                    logger.error(
                        "Project : %s",
                        job.project,
                    )

                    logger.error(
                        "Job ID : %s",
                        job.job_id,
                    )

                    logger.error(
                        "Directory : %s",
                        project_dir,
                    )

                    logger.error(
                        "Command : %s",
                        " ".join(command),
                    )

                    logger.error(
                        "Exit code : %s",
                        process.returncode,
                    )

                    logger.error(
                        "Error : %s",
                        job.error,
                    )

                    logger.error(
                        "============================================================"
                    )

                    await self.notifier.send(
                        (
                            f"FAILED | {job.project}\n"
                            f"Job: {job.job_id}\n"
                            f"{job.error}\n\n"
                            f"Output tail:\n"
                            f"{job.output_tail or '(no output)'}"
                        ),
                        chat_id,
                    )

                    logger.info(
                        "FAILED notification sent | "
                        "job_id=%s | project=%s",
                        job.job_id,
                        job.project,
                    )

            # =================================================
            # Timeout
            # =================================================

            except asyncio.TimeoutError:

                logger.error(
                    "============================================================"
                )

                logger.error(
                    "PROJECT UPDATE TIMED OUT"
                )

                logger.error(
                    "Project : %s",
                    job.project,
                )

                logger.error(
                    "Job ID : %s",
                    job.job_id,
                )

                logger.error(
                    "Directory : %s",
                    project_dir,
                )

                logger.error(
                    "Command : %s",
                    " ".join(command),
                )

                logger.error(
                    "Timeout : %s seconds",
                    self.settings.command_timeout_seconds,
                )

                logger.error(
                    "============================================================"
                )

                if (
                    process is not None
                    and process.returncode is None
                ):

                    logger.warning(
                        "Killing timed-out subprocess | "
                        "job_id=%s | pid=%s",
                        job.job_id,
                        process.pid,
                    )

                    process.kill()

                    await process.wait()

                    logger.info(
                        "Timed-out subprocess killed | "
                        "job_id=%s | pid=%s",
                        job.job_id,
                        process.pid,
                    )

                job.status = "timed_out"
                job.finished_at = utcnow()
                job.error = (
                    "Command timed out and was killed"
                )

                # ------------------------------------------------
                # Capture output
                # ------------------------------------------------

                if output:

                    text = output.decode(
                        errors="replace"
                    )

                    job.output_tail = text[
                        -self.settings.telegram_output_max_chars:
                    ]

                await self.notifier.send(
                    (
                        f"TIMEOUT | {job.project}\n"
                        f"Job: {job.job_id}\n"
                        f"{job.error}\n\n"
                        f"Output tail:\n"
                        f"{job.output_tail or '(no output)'}"
                    ),
                    chat_id,
                )

                logger.info(
                    "TIMEOUT notification sent | "
                    "job_id=%s | project=%s",
                    job.job_id,
                    job.project,
                )

            # =================================================
            # Unexpected Error
            # =================================================

            except Exception as exc:

                job.status = "failed"
                job.finished_at = utcnow()
                job.error = str(exc)

                logger.exception(
                    "============================================================"
                )

                logger.exception(
                    "UNEXPECTED PROJECT UPDATE ERROR"
                )

                logger.exception(
                    "Project : %s",
                    job.project,
                )

                logger.exception(
                    "Job ID : %s",
                    job.job_id,
                )

                logger.exception(
                    "Directory : %s",
                    project_dir,
                )

                logger.exception(
                    "Command : %s",
                    " ".join(command),
                )

                logger.exception(
                    "Error : %s",
                    exc,
                )

                logger.exception(
                    "============================================================"
                )

                # ------------------------------------------------
                # Capture output
                # ------------------------------------------------

                if output:

                    text = output.decode(
                        errors="replace"
                    )

                    job.output_tail = text[
                        -self.settings.telegram_output_max_chars:
                    ]

                await self.notifier.send(
                    (
                        f"FAILED | {job.project}\n"
                        f"Job: {job.job_id}\n"
                        f"Unexpected error: {exc}\n\n"
                        f"Output tail:\n"
                        f"{job.output_tail or '(no output)'}"
                    ),
                    chat_id,
                )

                logger.info(
                    "FAILED notification sent after exception | "
                    "job_id=%s | project=%s",
                    job.job_id,
                    job.project,
                )

            finally:

                logger.info(
                    "============================================================"
                )

                logger.info(
                    "JOB EXECUTION FINISHED"
                )

                logger.info(
                    "Project : %s",
                    job.project,
                )

                logger.info(
                    "Job ID : %s",
                    job.job_id,
                )

                logger.info(
                    "Final status : %s",
                    job.status,
                )

                logger.info(
                    "Project folder : %s",
                    project_dir,
                )

                logger.info(
                    "============================================================"
                ) 