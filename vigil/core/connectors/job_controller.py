import asyncio
import logging
import time
from typing import Any, Callable, Optional, Tuple

from vigil.core.common.ssh_connector import SSHConnection

FLUSH_INTERVAL = 0.5

FLUSH_LINES = 50


class JobRejected(Exception):
    pass


class JobController:
    def __init__(self, ssh_conn: SSHConnection, db: Any, plugin_id: str, target: str):
        self.ssh = ssh_conn
        self.db = db
        self.plugin_id = plugin_id
        self.target = target
        self._current_job: Optional[int] = None
        self._cancel = asyncio.Event()


    def is_running(self) -> bool:
        return self._current_job is not None

    def current_job_id(self) -> Optional[int]:
        return self._current_job


    async def run_job(self, kind: str, command: str, redacted: Optional[str] = None,
                      on_line: Optional[Callable[[str, str], None]] = None,
                      timeout: Optional[float] = None) -> Tuple[int, int]:
        if self._current_job is not None:
            raise JobRejected(
                f"A {kind} job is already running for this monitor"
            )
        self._cancel.clear()
        job_id = self.db.create_job(
            plugin_id=self.plugin_id, target=self.target, kind=kind,
            command=redacted if redacted is not None else command,
        )
        self._current_job = job_id

        try:
            return job_id, await self._execute(job_id, command, on_line, timeout)
        finally:
            self._current_job = None

    async def _execute(self, job_id: int, command: str,
                       on_line: Optional[Callable[[str, str], None]],
                       timeout: Optional[float]) -> int:
        buffer = []
        last_flush = time.monotonic()

        def flush():
            nonlocal buffer, last_flush
            if buffer:
                try:
                    self.db.append_job_output(job_id, buffer)
                except Exception as e:
                    logging.error(f"Failed to persist job {job_id} output: {e}")
                buffer = []
            last_flush = time.monotonic()

        def handle(stream: str, text: str):
            nonlocal buffer
            if on_line is not None:
                try:
                    on_line(stream, text)
                except Exception as e:
                    logging.error(f"Job {job_id} line handler failed: {e}")
            buffer.append(text)
            if len(buffer) >= FLUSH_LINES or (time.monotonic() - last_flush) >= FLUSH_INTERVAL:
                flush()

        try:
            status, error = await self.ssh.execute_streaming(
                command,
                on_line=handle,
                timeout=timeout,
                should_cancel=self._cancel.is_set,
            )
        except Exception as e:
            flush()
            logging.error(f"Job {job_id} crashed: {e}")
            self.db.finish_job(job_id, 'failed', exit_code=-1, error=str(e))
            return -1

        flush()

        if self._cancel.is_set():
            self.db.finish_job(job_id, 'cancelled', exit_code=status,
                               error='Cancelled by user')
        elif status == 0:
            self.db.finish_job(job_id, 'succeeded', exit_code=0)
        else:
            self.db.finish_job(job_id, 'failed', exit_code=status,
                               error=error or f"Exited with status {status}")
        return status

    def cancel(self) -> bool:
        if self._current_job is None:
            return False
        self._cancel.set()
        return True


    def recent(self, limit: int = 20, kind: Optional[str] = None) -> list:
        return self.db.recent_jobs(plugin_id=self.plugin_id, limit=limit, kind=kind)

    def output(self, job_id: int, after_seq: int = -1, limit: int = 500) -> list:
        return self.db.job_output(job_id, after_seq=after_seq, limit=limit)
