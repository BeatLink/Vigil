"""
Tests for the long-running job runner (Phase 1 infrastructure).

The JobController is exercised against a real temp database — job state is
meant to be durable across restarts and browser sessions, so persistence is
the behaviour under test, not an implementation detail to mock away.
"""
import asyncio
import pytest
from unittest.mock import MagicMock

from vigil.core.modules.controllers.job_controller import JobController, JobRejected


@pytest.fixture
def controller(db_manager):
    ssh = MagicMock()
    ssh.execute_streaming = MagicMock(return_value=(0, ""))
    return JobController(ssh, db_manager, "test-plugin", "test.host")


def _streaming(lines, status=0, error=""):
    """Fake execute_streaming that emits `lines` then exits with `status`."""
    def run(command, on_line=None, timeout=None, should_cancel=None):
        for line in lines:
            if on_line:
                on_line("stdout", line)
            if should_cancel and should_cancel():
                return 130, "Cancelled"
        return status, error
    return run


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    async def test_successful_job_is_recorded(self, controller, db_manager):
        controller.ssh.execute_streaming = _streaming(["a", "b"])
        job_id, exit_code = await controller.run_job("backup", "borg create ...")

        assert exit_code == 0
        job = db_manager.get_job(job_id)
        assert job['state'] == 'succeeded'
        assert job['exit_code'] == 0
        assert job['finished'] is not None
        assert job['running'] is False

    async def test_failed_job_records_exit_code(self, controller, db_manager):
        controller.ssh.execute_streaming = _streaming(["boom"], status=2, error="bad repo")
        job_id, exit_code = await controller.run_job("backup", "borg create ...")

        assert exit_code == 2
        job = db_manager.get_job(job_id)
        assert job['state'] == 'failed'
        assert job['exit_code'] == 2
        assert "bad repo" in job['error']

    async def test_output_lines_are_persisted_in_order(self, controller, db_manager):
        controller.ssh.execute_streaming = _streaming(["first", "second", "third"])
        job_id, _ = await controller.run_job("backup", "cmd")

        output = db_manager.job_output(job_id)
        assert [o['message'] for o in output] == ["first", "second", "third"]
        assert [o['seq'] for o in output] == [0, 1, 2]

    async def test_output_can_be_polled_incrementally(self, controller, db_manager):
        # The UI polls with the last seq it rendered; only newer lines return.
        controller.ssh.execute_streaming = _streaming(["a", "b", "c"])
        job_id, _ = await controller.run_job("backup", "cmd")

        assert [o['message'] for o in db_manager.job_output(job_id, after_seq=0)] == ["b", "c"]
        assert db_manager.job_output(job_id, after_seq=2) == []

    async def test_job_is_not_running_after_completion(self, controller):
        controller.ssh.execute_streaming = _streaming(["x"])
        await controller.run_job("backup", "cmd")
        assert controller.is_running() is False
        assert controller.current_job_id() is None

    async def test_crash_marks_job_failed(self, controller, db_manager):
        def explode(command, on_line=None, timeout=None, should_cancel=None):
            raise OSError("ssh binary missing")
        controller.ssh.execute_streaming = explode

        job_id, exit_code = await controller.run_job("backup", "cmd")
        assert exit_code == -1
        job = db_manager.get_job(job_id)
        assert job['state'] == 'failed'
        assert "ssh binary missing" in job['error']


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    async def test_second_job_is_rejected_while_one_runs(self, controller):
        started = asyncio.Event()
        release = asyncio.Event()

        def slow(command, on_line=None, timeout=None, should_cancel=None):
            started.set()
            # Block the worker thread until the test lets it finish.
            asyncio.run_coroutine_threadsafe(
                release.wait(), loop
            ).result(timeout=5)
            return 0, ""

        loop = asyncio.get_running_loop()
        controller.ssh.execute_streaming = slow

        task = asyncio.create_task(controller.run_job("backup", "cmd"))
        await asyncio.wait_for(started.wait(), timeout=5)

        # borg holds an exclusive repo lock; a second run must be refused here
        # rather than failing confusingly inside borg.
        with pytest.raises(JobRejected):
            await controller.run_job("backup", "cmd2")

        loop.call_soon_threadsafe(release.set)
        await task

    async def test_job_runs_after_previous_finishes(self, controller):
        controller.ssh.execute_streaming = _streaming(["x"])
        await controller.run_job("backup", "cmd")
        # The slot must be released, not held for the process lifetime.
        job_id, exit_code = await controller.run_job("backup", "cmd")
        assert exit_code == 0
        assert job_id is not None


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

class TestCancellation:
    async def test_cancel_returns_false_when_idle(self, controller):
        assert controller.cancel() is False

    async def test_cancelled_job_is_marked_cancelled(self, controller, db_manager):
        def cancellable(command, on_line=None, timeout=None, should_cancel=None):
            on_line("stdout", "working")
            controller.cancel()          # user hits cancel mid-stream
            if should_cancel():
                return 130, "Cancelled"
            return 0, ""

        controller.ssh.execute_streaming = cancellable
        job_id, _ = await controller.run_job("backup", "cmd")

        job = db_manager.get_job(job_id)
        assert job['state'] == 'cancelled'
        assert job['error'] == 'Cancelled by user'


# ---------------------------------------------------------------------------
# History and reconciliation
# ---------------------------------------------------------------------------

class TestHistory:
    async def test_recent_returns_newest_first(self, controller, db_manager):
        controller.ssh.execute_streaming = _streaming(["x"])
        first, _ = await controller.run_job("backup", "cmd1")
        second, _ = await controller.run_job("check", "cmd2")

        recent = controller.recent()
        assert [j['id'] for j in recent] == [second, first]

    async def test_recent_can_filter_by_kind(self, controller):
        controller.ssh.execute_streaming = _streaming(["x"])
        await controller.run_job("backup", "cmd1")
        await controller.run_job("check", "cmd2")

        assert [j['kind'] for j in controller.recent(kind="check")] == ["check"]

    async def test_redacted_command_is_stored_not_raw(self, controller, db_manager):
        controller.ssh.execute_streaming = _streaming(["x"])
        job_id, _ = await controller.run_job(
            "backup",
            command="BORG_PASSPHRASE=s3cret borg create",
            redacted="BORG_PASSPHRASE=***** borg create",
        )
        job = db_manager.get_job(job_id)
        # The job row is shown in the UI and retained; the secret must not be in it.
        assert "s3cret" not in job['command']
        assert "*****" in job['command']

    def test_orphaned_jobs_are_failed_on_restart(self, db_manager):
        # A job whose Vigil process died is still 'running' in the DB; startup
        # reconciliation must not leave it presented as live.
        job_id = db_manager.create_job("p", "h", "backup", "cmd")
        assert db_manager.get_job(job_id)['running'] is True

        updated = db_manager.reconcile_orphaned_jobs()

        assert updated == 1
        job = db_manager.get_job(job_id)
        assert job['state'] == 'failed'
        assert "restarted" in job['error']

    def test_reconcile_leaves_finished_jobs_alone(self, db_manager):
        job_id = db_manager.create_job("p", "h", "backup", "cmd")
        db_manager.finish_job(job_id, 'succeeded', exit_code=0)

        db_manager.reconcile_orphaned_jobs()

        assert db_manager.get_job(job_id)['state'] == 'succeeded'

    def test_running_jobs_lists_only_active(self, db_manager):
        done = db_manager.create_job("p", "h", "backup", "cmd")
        db_manager.finish_job(done, 'succeeded', exit_code=0)
        active = db_manager.create_job("p", "h", "backup", "cmd")

        assert [j['id'] for j in db_manager.running_jobs()] == [active]

    def test_progress_is_replaced_not_appended(self, db_manager):
        job_id = db_manager.create_job("p", "h", "backup", "cmd")
        db_manager.set_job_progress(job_id, "10 files")
        db_manager.set_job_progress(job_id, "20 files")
        assert db_manager.get_job(job_id)['progress'] == "20 files"
