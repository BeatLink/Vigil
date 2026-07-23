import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from vigil.core.common.ssh_connector import SSHConnection

# How often the reader thread flushes buffered output lines to the database.
# Output arrives far faster than one-row-per-commit can absorb (borg --progress
# emits continuously), so lines are batched; half a second keeps the UI feeling
# live while collapsing a burst into a single write.
FLUSH_INTERVAL = 0.5

# Maximum lines held before forcing a flush regardless of FLUSH_INTERVAL, so a
# fast-talking command cannot balloon the buffer between ticks.
FLUSH_LINES = 50


class JobRejected(Exception):
    """Raised when a job cannot be started (one is already running)."""


class JobController:
    """
    Runs long-lived commands on a target host as tracked, cancellable jobs.

    SSHController exists for the opposite case: a short remediation command with
    a 30-second ceiling whose only result is a boolean. That model cannot
    express a borg backup — it runs for hours, produces output worth watching
    while it runs, and must survive the browser session that started it.

    A job here is a database row (see database.Job) plus a background thread
    draining the remote process's output into it. Nothing about a job lives only
    in memory, so the UI reattaches after a reload by reading the row, and a
    second browser sees the same state. The controller holds one job at a time
    per plugin: borg takes an exclusive repository lock, so a concurrent backup
    would fail on the lock anyway — rejecting it here gives a clear error
    instead of a confusing one from borg.
    """

    def __init__(self, ssh_conn: SSHConnection, db: Any, plugin_id: str, target: str):
        self.ssh = ssh_conn
        self.db = db
        self.plugin_id = plugin_id
        self.target = target
        # Guards _current_job / _cancel against the reader thread finishing at
        # the same moment the UI starts or cancels a job.
        self._lock = threading.Lock()
        self._current_job: Optional[int] = None
        self._cancel = threading.Event()

    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------

    def is_running(self) -> bool:
        """True if this controller is currently executing a job."""
        with self._lock:
            return self._current_job is not None

    def current_job_id(self) -> Optional[int]:
        """The id of the running job, or None."""
        with self._lock:
            return self._current_job

    # -------------------------------------------------------------------------
    # Execution
    # -------------------------------------------------------------------------

    async def run_job(self, kind: str, command: str, redacted: Optional[str] = None,
                      on_line: Optional[Callable[[str, str], None]] = None,
                      timeout: Optional[float] = None) -> Tuple[int, int]:
        """
        Start a job and wait for it to finish. Returns (job_id, exit_code).

        `redacted` is what gets persisted as the job's command — callers passing
        a command with an inlined secret must supply it, since the row is shown
        in the UI and kept after the job ends. It defaults to `command`, which is
        only safe when the command carries no secret.

        `on_line(stream, text)` is invoked for each output line before it is
        stored, letting a caller parse structured progress out of the stream.

        The blocking work runs in a worker thread; awaiting this coroutine does
        not occupy the event loop. Raises JobRejected if a job is already running.
        """
        with self._lock:
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
            exit_code = await asyncio.get_event_loop().run_in_executor(
                None, self._execute, job_id, command, on_line, timeout
            )
            return job_id, exit_code
        finally:
            with self._lock:
                self._current_job = None

    def _execute(self, job_id: int, command: str,
                 on_line: Optional[Callable[[str, str], None]],
                 timeout: Optional[float]) -> int:
        """
        Blocking body of a job, run in a worker thread.

        Buffers output lines and flushes them on a timer so a chatty command
        does not turn into one DB commit per line, while the UI still sees
        progress within FLUSH_INTERVAL.
        """
        buffer = []
        last_flush = time.monotonic()

        def flush():
            nonlocal buffer, last_flush
            if buffer:
                try:
                    self.db.append_job_output(job_id, buffer)
                except Exception as e:
                    # Losing an output line must not abort the job itself.
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
            status, error = self.ssh.execute_streaming(
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

        flush()  # anything buffered after the final line

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
        """
        Request cancellation of the running job.

        Sets a flag the reader thread checks between output lines; it then
        terminates the remote process. Returns False if no job is running.
        Cancellation is therefore observed at the next line of output — for
        borg, which reports progress continuously, that is effectively immediate.
        """
        with self._lock:
            if self._current_job is None:
                return False
            self._cancel.set()
            return True

    # -------------------------------------------------------------------------
    # History
    # -------------------------------------------------------------------------

    def recent(self, limit: int = 20, kind: Optional[str] = None) -> list:
        """Recent jobs for this plugin, newest first."""
        return self.db.recent_jobs(plugin_id=self.plugin_id, limit=limit, kind=kind)

    def output(self, job_id: int, after_seq: int = -1, limit: int = 500) -> list:
        """Output lines for a job after `after_seq` (for incremental polling)."""
        return self.db.job_output(job_id, after_seq=after_seq, limit=limit)
