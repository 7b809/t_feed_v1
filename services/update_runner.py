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

def utcnow():
    return datetime.now(timezone.utc)

class UpdateManager:
    def __init__(self, settings: Settings, notifier: TelegramNotifier):
        self.settings = settings
        self.notifier = notifier
        self.jobs: dict[str, JobStatus] = {}
        self.locks = {name: asyncio.Lock() for name in settings.projects}
        self.tasks: set[asyncio.Task] = set()

    def submit(self, project: str, requested_by: str, chat_id: int | None = None) -> JobStatus:
        if project not in self.settings.projects:
            raise KeyError(project)
        job = JobStatus(job_id=uuid.uuid4().hex, project=project, status="queued", requested_by=requested_by, created_at=utcnow())
        self.jobs[job.job_id] = job
        task = asyncio.create_task(self._run(job, chat_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    def get(self, job_id: str) -> JobStatus | None:
        return self.jobs.get(job_id)

    async def _run(self, job: JobStatus, chat_id: int | None):
        await self.notifier.send(f"QUEUE | {job.project}\nJob: {job.job_id}\nRequested by: {job.requested_by}", chat_id)
        async with self.locks[job.project]:
            project_dir = self.settings.projects[job.project]
            script = project_dir / self.settings.update_script
            command = [self.settings.update_python, self.settings.update_script]
            if not project_dir.is_dir() or not script.is_file():
                job.status, job.finished_at, job.error = "failed", utcnow(), f"Missing directory or script: {script}"
                logger.error("%s | %s", job.job_id, job.error)
                await self.notifier.send(f"FAILED | {job.project}\n{job.error}", chat_id)
                return
            job.status, job.started_at = "running", utcnow()
            await self.notifier.send(f"START | {job.project}\nCommand: {' '.join(command)}\nDirectory: {project_dir}", chat_id)
            logger.info("%s | starting %s in %s", job.job_id, command, project_dir)
            try:
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                process = await asyncio.create_subprocess_exec(*command, cwd=project_dir, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                output = bytearray()
                async def read_output():
                    assert process.stdout
                    while True:
                        chunk = await process.stdout.read(4096)
                        if not chunk: break
                        output.extend(chunk)
                        logger.info("%s | %s", job.job_id, chunk.decode(errors="replace").rstrip())
                await asyncio.wait_for(asyncio.gather(process.wait(), read_output()), timeout=self.settings.command_timeout_seconds)
                text = output.decode(errors="replace")
                job.output_tail = text[-self.settings.telegram_output_max_chars:]
                job.return_code = process.returncode
                job.finished_at = utcnow()
                if process.returncode == 0:
                    job.status = "succeeded"
                    await self.notifier.send(f"SUCCESS | {job.project}\nJob: {job.job_id}\nExit code: 0\n\nOutput tail:\n{job.output_tail or '(no output)'}", chat_id)
                else:
                    job.status, job.error = "failed", f"Command exited with code {process.returncode}"
                    await self.notifier.send(f"FAILED | {job.project}\nJob: {job.job_id}\n{job.error}\n\nOutput tail:\n{job.output_tail}", chat_id)
            except asyncio.TimeoutError:
                if 'process' in locals() and process.returncode is None:
                    process.kill(); await process.wait()
                job.status, job.finished_at, job.error = "timed_out", utcnow(), "Command timed out and was killed"
                await self.notifier.send(f"TIMEOUT | {job.project}\nJob: {job.job_id}\n{job.error}", chat_id)
            except Exception as exc:
                job.status, job.finished_at, job.error = "failed", utcnow(), str(exc)
                logger.exception("%s | unexpected failure", job.job_id)
                await self.notifier.send(f"FAILED | {job.project}\nJob: {job.job_id}\nUnexpected error: {exc}", chat_id)
