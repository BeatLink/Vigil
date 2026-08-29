"""JobsGateway: the narrow job-control surface a job-capable plugin and its UI
hold, in place of pass-throughs on the engine. A job is a detached command on
the target (a Job row) advanced by the owning plugin's poll; "running" is a DB
state, not a live coroutine, so everything here is DB reads and writes plus
(for cancel) one ordinary SSH command on the plugin's transport."""

from typing import Awaitable, Callable, Optional


class JobsGateway:
    """Detached-job state and control scoped to one plugin."""

    def __init__(self, db, plugin, cancel_exec: Optional[Callable[[str], Awaitable]] = None):
        """Bind the gateway to the store and one plugin; cancel_exec runs a raw command on the plugin's transport."""
        self._db = db
        self._plugin = plugin
        self._cancel_exec = cancel_exec

    def running(self) -> Optional[dict]:
        """Return the plugin's currently running job row, if any."""
        jobs = self._db.running_jobs(plugin_id=self._plugin.id)
        return jobs[0] if jobs else None

    def is_running(self) -> bool:
        """Whether the plugin has a running job."""
        return self.running() is not None

    def current_id(self) -> Optional[int]:
        """Return the running job's id, if any."""
        job = self.running()
        return job['id'] if job else None

    def recent(self, limit: int = 20) -> list:
        """Return the plugin's most recent job rows."""
        return self._db.recent_jobs(plugin_id=self._plugin.id, limit=limit)

    async def cancel(self) -> bool:
        """Kill the plugin's running detached job on the target (one ordinary
        SSH command) and mark it cancelled. The plugin's next poll would also
        observe the death, but cancelling eagerly gives immediate feedback."""
        from vigil.core.connectors.ssh_connector import cancel_command
        job = self.running()
        if not job or not job.get('pid'):
            return False
        if self._cancel_exec is not None:
            await self._cancel_exec(cancel_command(job['pid']))
        self._db.finish_job(job['id'], 'cancelled', exit_code=130, error='Cancelled by user')
        return True

    def create(self, kind: str, command: str, workdir: str) -> int:
        """Record a newly launched detached job and return its id."""
        return self._db.create_job(plugin_id=self._plugin.id, target=self._plugin.target,
                                   kind=kind, command=command, workdir=workdir)

    def set_pid(self, job_id: int, pid: int) -> None:
        """Record the detached job's process-group id once known."""
        self._db.set_job_pid(job_id, pid)

    def set_progress(self, job_id: int, summary: str) -> None:
        """Update the one-line progress summary the job panel shows."""
        self._db.set_job_progress(job_id, summary)

    def append_output(self, job_id: int, lines: list) -> None:
        """Append newly observed output lines to the job's log."""
        self._db.append_job_output(job_id, lines)

    def bump_output_seq(self, job_id: int, new_seq: int) -> None:
        """Advance the byte offset up to which the job's output was consumed."""
        self._db.bump_job_output_seq(job_id, new_seq)

    def finish(self, job_id: int, state: str, exit_code=None, error=None) -> None:
        """Mark the job finished with its terminal state."""
        self._db.finish_job(job_id, state, exit_code=exit_code, error=error)
