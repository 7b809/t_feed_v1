
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
            "Job submission requested | project=%s | requested_by=%s | chat_id=%s",
            project,
            requested_by,
            chat_id,
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
            "Job execution started | job_id=%s | project=%s | chat_id=%s",
            job.job_id,
            job.project,
            chat_id,
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

        logger.info(
            "Project configuration loaded | "
            "job_id=%s | project=%s | folder=%s | command=%s",
            job.job_id,
            job.project,
            project_dir,
            command,
        )

        # ----------------------------------------------------
        # Lock project
        # ----------------------------------------------------

        logger.info(
            "Waiting for project lock | "
            "job_id=%s | project=%s",
            job.job_id,
            job.project,
        )

        async with self.locks[job.project]:

            logger.info(
                "Project lock acquired | "
                "job_id=%s | project=%s",
                job.job_id,
                job.project,
            )

            # ------------------------------------------------
            # Validate project directory
            # ------------------------------------------------

            if not project_dir.is_dir():

                error = (
                    f"Project directory does not exist: "
                    f"{project_dir}"
                )

                job.status = "failed"
                job.finished_at = utcnow()
                job.error = error

                logger.error(
                    "Job failed | job_id=%s | project=%s | error=%s",
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
                    "Job failed | job_id=%s | project=%s | error=%s",
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

            # ------------------------------------------------
            # Validate executable/script when possible
            # ------------------------------------------------

            executable = command[0]

            if len(command) >= 2:

                possible_script = project_dir / command[1]

                if (
                    command[0] in (
                        "python",
                        "python3",
                                           "bash",
                        "sh",
                    )
                    and not possible_script.is_file()
                ):

                    error = (
                        f"Required script not found: "
                        f"{possible_script}"
                    )

                    job.status = "failed"
                    job.finished_at = utcnow()
                    job.error = error

                    logger.error(
                        "Job failed | job_id=%s | project=%s | error=%s",
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

            # ------------------------------------------------
            # Mark job as running
            # ------------------------------------------------

            job.status = "running"
            job.started_at = utcnow()

            logger.info(
                "Job running | job_id=%s | project=%s | "
                "folder=%s | command=%s",
                job.job_id,
                job.project,
                project_dir,
                command,
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
                    "Starting subprocess | "
                    "job_id=%s | command=%s | cwd=%s",
                    job.job_id,
                    command,
                    project_dir,
                )

                # ============================================
                # Start process
                # ============================================

                process = (
                    await asyncio.create_subprocess_exec(
                        *command,
                        cwd=project_dir,
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                )

                logger.info(
                    "Subprocess started | "
                    "job_id=%s | pid=%s | project=%s",
                    job.job_id,
                    process.pid,
                    job.project,
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
                                "%s | OUTPUT | %s",
                                job.job_id,
                                text,
                            )

                # ============================================
                # Wait for process + output
                # ============================================

                logger.info(
                    "Waiting for subprocess | "
                    "job_id=%s | timeout=%s seconds",
                    job.job_id,
                    self.settings.command_timeout_seconds,
                )

                await asyncio.wait_for(
                    asyncio.gather(
                        process.wait(),
                        read_output(),
                    ),
                    timeout=(
                        self.settings
                        .command_timeout_seconds
                    ),
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
                    "Subprocess completed | "
                    "job_id=%s | project=%s | pid=%s | "
                    "return_code=%s",
                    job.job_id,
                    job.project,
                    process.pid,
                    process.returncode,
                )

                # ============================================
                # Success
                # ============================================

                if process.returncode == 0:

                    job.status = "succeeded"

                    logger.info(
                        "Job succeeded | "
                        "job_id=%s | project=%s | "
                        "return_code=0",
                        job.job_id,
                        job.project,
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
                        "Job failed | "
                        "job_id=%s | project=%s | "
                        "return_code=%s | error=%s",
                        job.job_id,
                        job.project,
                        process.returncode,
                        job.error,
                    )

                    await self.notifier.send(
                        (
                            f"FAILED | {job.project}\n"
                            f"Job: {job.job_id}\n"
                            f"{job.error}\n\n"
                            f"Output tail:\n"
                            f"{job.output_tail}"
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
                    "Job timed out | "
                    "job_id=%s | project=%s | "
                    "timeout=%s seconds",
                    job.job_id,
                    job.project,
                    self.settings.command_timeout_seconds,
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

                job.status = "timed_out"
                job.finished_at = utcnow()
                job.error = (
                    "Command timed out and was killed"
                )

                # ------------------------------------------------
                # Capture any output already produced
                # ------------------------------------------------

                if output:

                    text = output.decode(
                        errors="replace"
                    )

                    job.output_tail = text[
                        -self.settings
                        .telegram_output_max_chars:
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
                    "Unexpected job failure | "
                    "job_id=%s | project=%s",
                    job.job_id,
                    job.project,
                )

                # ------------------------------------------------
                # Capture any output already produced
                # ------------------------------------------------

                if output:

                    text = output.decode(
                        errors="replace"
                    )

                    job.output_tail = text[
                        -self.settings
                        .telegram_output_max_chars:
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
                    "Job execution finished | "
                    "job_id=%s | project=%s | status=%s",
                    job.job_id,
                    job.project,
                    job.status,
                ) 