"""Detached-job helpers: pure shell-command builders and result parsers, plus
the DB reconcile/re-adopt behavior. A job is now an ordinary detached command
on the target, polled through the normal SSH command path — there is no
long-lived controller, so these are pure functions and DB-state tests."""

import subprocess
import time

from vigil.core.connectors import ssh_connector as jobs


def _wait_for(path, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return path.exists()


class TestLaunch:
    def test_launched_job_really_runs_and_records_its_exit(self, tmp_path):
        """Executes the launch script under sh: the detached child is a separate
        process, so the workdir variable has to reach it or nothing runs."""
        cmd = jobs.launch_command("echo ran; (exit 3)", jobs.workdir_for("real-1"))
        proc = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True,
                              env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/run/current-system/sw/bin"})
        assert jobs.parse_launch(proc.stdout) is not None
        workdir = tmp_path / ".cache" / "vigil" / "jobs" / "real-1"
        assert _wait_for(workdir / "exit"), "the detached command never finished"
        assert (workdir / "out").read_text() == "ran\n"
        assert (workdir / "exit").read_text().strip() == "3"

    def test_launch_command_detaches_and_echoes_pid(self):
        cmd = jobs.launch_command("borg create repo::x /data", "$HOME/.cache/vigil/jobs/tok")
        assert "setsid" in cmd
        assert "borg create repo::x /data" in cmd
        assert 'echo $!' in cmd            # prints the remote pid
        assert '"$d/exit"' in cmd          # captures exit status
        assert '"$d/out"' in cmd           # captures output

    def test_parse_launch_reads_pid(self):
        assert jobs.parse_launch("12345\n") == 12345
        assert jobs.parse_launch("some warning\n67890\n") == 67890

    def test_parse_launch_none_on_garbage(self):
        assert jobs.parse_launch("permission denied") is None
        assert jobs.parse_launch("") is None

    def test_workdir_is_sanitized(self):
        wd = jobs.workdir_for("borg-monitor-1700000000")
        assert wd.endswith("borg-monitor-1700000000")
        wd2 = jobs.workdir_for("../etc/passwd; rm -rf /")
        assert ";" not in wd2 and "/" not in wd2.rsplit("/", 1)[1]


class TestPoll:
    def _poll_output(self, size, exit_code, alive, out):
        return (
            f"===VIGIL_SIZE===\n{size}\n"
            f"===VIGIL_EXIT===\n{exit_code if exit_code is not None else ''}\n"
            f"===VIGIL_ALIVE===\n{1 if alive else 0}\n"
            f"===VIGIL_OUT===\n{out}"
        )

    def test_poll_command_uses_offset_and_pid(self):
        cmd = jobs.poll_command("$HOME/.cache/vigil/jobs/tok", 4242, offset=100)
        assert "kill -0 4242" in cmd
        assert "tail -c +101" in cmd       # offset+1 (tail -c is 1-indexed)
        assert "wc -c" in cmd

    def test_parse_poll_running(self):
        r = jobs.parse_poll(self._poll_output(500, None, True, "progress line\n"))
        assert r.size == 500
        assert r.exit_code is None
        assert r.alive is True
        assert "progress line" in r.new_output

    def test_parse_poll_completed(self):
        r = jobs.parse_poll(self._poll_output(1000, 0, False, "done\n"))
        assert r.exit_code == 0
        assert r.alive is False

    def test_parse_poll_failed_exit(self):
        r = jobs.parse_poll(self._poll_output(0, 2, False, ""))
        assert r.exit_code == 2

    def test_parse_poll_dead_without_exit_file(self):
        # process vanished (target reboot) — no exit code, not alive
        r = jobs.parse_poll(self._poll_output(50, None, False, "partial"))
        assert r.exit_code is None
        assert r.alive is False


class TestSplitLines:
    def test_whole_lines_split_and_consumed(self):
        lines, consumed = jobs.split_lines("a\nb\nc\n")
        assert lines == ["a", "b", "c"]
        assert consumed == 6

    def test_trailing_partial_line_left_unconsumed(self):
        lines, consumed = jobs.split_lines("a\nb\npartial")
        assert lines == ["a", "b"]
        assert consumed == 4                # only "a\nb\n" consumed
        # the next poll re-reads from offset 4 and completes "partial"

    def test_empty_output(self):
        assert jobs.split_lines("") == ([], 0)
        assert jobs.split_lines("no newline yet") == ([], 0)


class TestCancelCommand:
    def test_cancel_escalates_term_then_kill(self):
        cmd = jobs.cancel_command(999)
        assert "kill 999" in cmd
        assert "kill -9 999" in cmd


class TestReconcileAndReadopt:
    def test_running_job_without_pid_is_failed_on_restart(self, db_manager):
        # created but never launched (no pid) → cannot be running remotely
        job_id = db_manager.create_job("p", "h", "backup", "cmd")
        updated = db_manager.reconcile_orphaned_jobs()
        assert updated == 1
        assert db_manager.get_job(job_id)['state'] == 'failed'

    def test_running_job_with_pid_is_kept_for_readoption(self, db_manager):
        job_id = db_manager.create_job("p", "h", "backup", "cmd", workdir="/wd")
        db_manager.set_job_pid(job_id, 4242)
        updated = db_manager.reconcile_orphaned_jobs()
        assert updated == 0                              # not force-failed
        assert db_manager.get_job(job_id)['state'] == 'running'
        assert [j['id'] for j in db_manager.running_jobs_with_pid()] == [job_id]

    def test_reconcile_leaves_finished_jobs_alone(self, db_manager):
        job_id = db_manager.create_job("p", "h", "backup", "cmd")
        db_manager.finish_job(job_id, 'succeeded', exit_code=0)
        db_manager.reconcile_orphaned_jobs()
        assert db_manager.get_job(job_id)['state'] == 'succeeded'


class TestJobDbState:
    def test_output_appended_in_order_and_polled_incrementally(self, db_manager):
        job_id = db_manager.create_job("p", "h", "backup", "cmd")
        db_manager.append_job_output(job_id, ["a", "b"])
        db_manager.append_job_output(job_id, ["c"])
        assert [o['message'] for o in db_manager.job_output(job_id)] == ["a", "b", "c"]
        assert [o['message'] for o in db_manager.job_output(job_id, after_seq=1)] == ["c"]

    def test_output_seq_offset_tracked(self, db_manager):
        job_id = db_manager.create_job("p", "h", "backup", "cmd", workdir="/wd")
        db_manager.bump_job_output_seq(job_id, 128)
        assert db_manager.get_job(job_id)['output_seq'] == 128

    def test_progress_is_replaced_not_appended(self, db_manager):
        job_id = db_manager.create_job("p", "h", "backup", "cmd")
        db_manager.set_job_progress(job_id, "10 files")
        db_manager.set_job_progress(job_id, "20 files")
        assert db_manager.get_job(job_id)['progress'] == "20 files"

    def test_recent_newest_first_and_filterable(self, db_manager):
        first = db_manager.create_job("p", "h", "backup", "cmd1")
        db_manager.finish_job(first, 'succeeded', exit_code=0)
        second = db_manager.create_job("p", "h", "check", "cmd2")
        db_manager.finish_job(second, 'succeeded', exit_code=0)
        assert [j['id'] for j in db_manager.recent_jobs(plugin_id="p")] == [second, first]
        assert [j['kind'] for j in db_manager.recent_jobs(plugin_id="p", kind="check")] == ["check"]
