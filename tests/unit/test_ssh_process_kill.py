"""
Tests that a timed-out command leaves nothing running behind.

subprocess.run's timeout kills only its direct child. In production that left
one wedged `ssh` (and its remote borg) per poll against a busy repository,
accumulating until the monitored host was saturated. These drive real process
trees rather than mocks, because the defect was in process handling itself.
"""
import os
import signal
import subprocess
import time

import pytest

from vigil.core.common.ssh_connector import SSHConnection


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class TestKillGroup:
    def test_kills_child_process(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        SSHConnection._kill_group(proc)
        proc.wait(timeout=5)
        assert not _alive(proc.pid)

    def test_kills_grandchildren_too(self):
        # The real tree is ssh -> sudo -> borg; killing only the direct child
        # leaves the rest running. Model it with a shell that forks a child.
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 60 & echo $!; wait"],
            stdout=subprocess.PIPE, start_new_session=True,
        )
        grandchild = int(proc.stdout.readline().strip())
        assert _alive(grandchild)

        SSHConnection._kill_group(proc)
        proc.wait(timeout=5)

        deadline = time.time() + 5
        while _alive(grandchild) and time.time() < deadline:
            time.sleep(0.05)
        assert not _alive(grandchild), "grandchild survived the group kill"

    def test_escalates_to_sigkill_when_sigterm_ignored(self):
        # borg can sit in uninterruptible I/O where SIGTERM does not land;
        # the kill must escalate rather than give up.
        proc = subprocess.Popen(
            ["sh", "-c", "trap '' TERM; sleep 60"], start_new_session=True,
        )
        time.sleep(0.2)   # let the trap install
        SSHConnection._kill_group(proc)
        proc.wait(timeout=20)
        assert not _alive(proc.pid)

    def test_noop_on_finished_process(self):
        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait(timeout=5)
        SSHConnection._kill_group(proc)   # must not raise

    def test_noop_on_none(self):
        SSHConnection._kill_group(None)   # must not raise


class TestExecuteTimeout:
    def test_timeout_reports_and_leaves_nothing_running(self, monkeypatch):
        conn = SSHConnection(host="example.invalid")
        # Replace the ssh argv with a local command that outlives the timeout
        # and forks, so the whole tree must be cleaned up.
        monkeypatch.setattr(
            conn, "_ssh_base",
            lambda ct: ["sh", "-c", "sleep 30 & sleep 30", "--"],
        )
        started = time.time()
        status, out, err = conn.execute("ignored", timeout=1.0)
        elapsed = time.time() - started

        assert status == -1
        assert "Timed out" in err
        # Must return promptly rather than waiting out the full sleep.
        assert elapsed < 15
