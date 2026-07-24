import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from vigil.core.common import ssh_connector
from vigil.core.common.ssh_connector import SSHConnection, _TofuClient


def _completed_process(exit_status=0, stdout="", stderr=""):
    result = MagicMock()
    result.exit_status = exit_status
    result.stdout = stdout
    result.stderr = stderr
    return result


def _mock_conn_and_proc(exit_status=0, stdout="", stderr="", wait_hangs=False):
    proc = MagicMock()
    proc.exit_status = None
    proc.is_closing.return_value = False

    async def wait():
        if wait_hangs:
            await asyncio.sleep(999)
        proc.exit_status = exit_status
        return _completed_process(exit_status, stdout, stderr)

    proc.wait = wait

    async def wait_closed():
        return None

    proc.wait_closed = wait_closed
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    conn = MagicMock()
    conn.is_closed.return_value = False

    async def create_process(*a, **kw):
        return proc

    conn.create_process = create_process
    return conn, proc


class TestFromConfig:
    def test_reads_host_from_ssh_config(self):
        conn = SSHConnection.from_config({"ssh_config": {"host": "myhost"}})
        assert conn.host == "myhost"

    def test_falls_back_to_target_host(self):
        conn = SSHConnection.from_config({"target_host": "fallback.host"})
        assert conn.host == "fallback.host"

    def test_defaults_to_localhost(self):
        conn = SSHConnection.from_config({})
        assert conn.host == "localhost"

    def test_reads_username(self):
        conn = SSHConnection.from_config({"ssh_config": {"host": "h", "username": "admin"}})
        assert conn.username == "admin"

    def test_default_port_is_22(self):
        conn = SSHConnection.from_config({"ssh_config": {"host": "h"}})
        assert conn.port == 22

    def test_custom_port(self):
        conn = SSHConnection.from_config({"ssh_config": {"host": "h", "port": 2222}})
        assert conn.port == 2222

    def test_new_instance_each_call(self):
        a = SSHConnection.from_config({"ssh_config": {"host": "h", "username": "u"}})
        b = SSHConnection.from_config({"ssh_config": {"host": "h", "username": "u"}})
        assert a is not b


class TestExecute:
    async def test_returns_exit_code_stdout_stderr(self):
        conn = SSHConnection("myhost", username="user")
        mock_conn, _ = _mock_conn_and_proc(0, "hello", "")
        with patch.object(conn, "_get_connection", AsyncMock(return_value=mock_conn)):
            rc, out, err = await conn.execute("echo hello")
        assert (rc, out, err) == (0, "hello", "")

    async def test_nonzero_exit_code_returned(self):
        conn = SSHConnection("myhost")
        mock_conn, _ = _mock_conn_and_proc(1, "", "not found")
        with patch.object(conn, "_get_connection", AsyncMock(return_value=mock_conn)):
            rc, out, err = await conn.execute("bad_cmd")
        assert rc == 1
        assert err == "not found"

    async def test_stdout_and_stderr_stay_separate(self):
        conn = SSHConnection("h")
        mock_conn, _ = _mock_conn_and_proc(0, "out-only", "err-only")
        with patch.object(conn, "_get_connection", AsyncMock(return_value=mock_conn)):
            rc, out, err = await conn.execute("cmd")
        assert out == "out-only"
        assert err == "err-only"

    async def test_no_pty_requested(self):
        conn = SSHConnection("h")
        mock_conn = MagicMock()
        create_process_spy = AsyncMock(return_value=_mock_conn_and_proc(0)[1])
        mock_conn.create_process = create_process_spy
        with patch.object(conn, "_get_connection", AsyncMock(return_value=mock_conn)):
            await conn.execute("cmd")
        assert "term_type" not in create_process_spy.call_args.kwargs

    async def test_timeout_kills_the_process(self):
        conn = SSHConnection("slowhost")
        mock_conn, proc = _mock_conn_and_proc(wait_hangs=True)
        with patch.object(conn, "_get_connection", AsyncMock(return_value=mock_conn)):
            rc, out, err = await conn.execute("sleep 999", timeout=0.05)
        assert rc == -1
        assert "Timed out" in err
        assert proc.terminate.called, "a timed-out command must be explicitly killed"

    async def test_connection_error_returns_sentinel(self):
        conn = SSHConnection("h")
        with patch.object(conn, "_get_connection", AsyncMock(side_effect=OSError("no route to host"))):
            rc, out, err = await conn.execute("cmd")
        assert rc == -1
        assert "no route to host" in err

    async def test_passes_timeout_to_wait(self):
        conn = SSHConnection("h")
        mock_conn, proc = _mock_conn_and_proc(0)
        with patch.object(conn, "_get_connection", AsyncMock(return_value=mock_conn)):
            rc, out, err = await conn.execute("cmd", timeout=12.5)
        assert rc == 0


class TestExecuteStreaming:
    async def test_cancellation_kills_the_process(self):
        conn = SSHConnection("h")
        proc = MagicMock()
        proc.exit_status = None
        proc.is_closing.return_value = False
        proc.stdout.at_eof.return_value = False

        async def readline():
            await asyncio.sleep(999)

        proc.stdout.readline = readline

        async def wait_closed():
            return None

        proc.wait_closed = wait_closed
        proc.terminate = MagicMock()

        mock_conn = MagicMock()

        async def create_process(*a, **kw):
            return proc

        mock_conn.create_process = create_process

        with patch.object(conn, "_get_connection", AsyncMock(return_value=mock_conn)):
            status, msg = await conn.execute_streaming("sleep 999", should_cancel=lambda: True)
        assert status == 130
        assert proc.terminate.called

    async def test_lines_delivered_to_callback(self):
        conn = SSHConnection("h")
        proc = MagicMock()
        proc.exit_status = 0
        proc.is_closing.return_value = False
        lines = iter(["first\n", "second\n", ""])

        async def readline():
            return next(lines)

        proc.stdout.readline = readline
        proc.stdout.at_eof.side_effect = lambda: proc.exit_status == 0 and next(iter([True]), True)

        async def wait():
            return _completed_process(0)

        proc.wait = wait

        mock_conn = MagicMock()

        async def create_process(*a, **kw):
            return proc

        mock_conn.create_process = create_process

        received = []
        with patch.object(conn, "_get_connection", AsyncMock(return_value=mock_conn)):
            status, _ = await conn.execute_streaming(
                "cmd", on_line=lambda stream, text: received.append((stream, text)),
            )
        assert ("stdout", "first") in received
        assert ("stdout", "second") in received


class TestKillProcess:
    async def test_terminate_succeeds_kill_not_called(self):
        proc = MagicMock()
        proc.exit_status = None
        proc.is_closing.return_value = False

        async def wait_closed():
            return None

        proc.wait_closed = wait_closed

        await SSHConnection._kill_process(proc)

        assert proc.terminate.called
        assert not proc.kill.called

    async def test_escalates_to_kill_when_terminate_does_not_close(self):
        proc = MagicMock()
        proc.exit_status = None
        proc.is_closing.return_value = False

        call_count = {"n": 0}

        async def wait_closed():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise asyncio.TimeoutError()
            return None

        proc.wait_closed = wait_closed

        await SSHConnection._kill_process(proc)

        assert proc.terminate.called
        assert proc.kill.called

    async def test_noop_on_already_finished_process(self):
        proc = MagicMock()
        proc.exit_status = 0
        proc.is_closing.return_value = False

        await SSHConnection._kill_process(proc)

        assert not proc.terminate.called
        assert not proc.kill.called

    async def test_noop_on_already_closing_process(self):
        proc = MagicMock()
        proc.exit_status = None
        proc.is_closing.return_value = True

        await SSHConnection._kill_process(proc)

        assert not proc.terminate.called


class TestConcurrencyBound:
    async def test_execute_channels_are_bounded_per_host(self):
        conn = SSHConnection("h")
        assert conn._channel_semaphore._value == ssh_connector._MAX_CONCURRENT_PER_HOST

    async def test_job_channels_use_a_separate_smaller_pool(self):
        conn = SSHConnection("h")
        assert conn._job_semaphore._value == ssh_connector._MAX_CONCURRENT_JOBS_PER_HOST
        assert ssh_connector._MAX_CONCURRENT_JOBS_PER_HOST < ssh_connector._MAX_CONCURRENT_PER_HOST


class TestTofuHostKeyValidation:
    def test_no_stored_key_trusts_and_persists(self, tmp_path):
        with patch.object(ssh_connector, "_STATE_DIR", tmp_path):
            client = _TofuClient("myhost", "user@myhost:22")
            key = MagicMock()
            key.get_fingerprint.return_value = "SHA256:abc"
            key.export_public_key.return_value = b"ssh-ed25519 AAAA...\n"

            assert client.validate_host_public_key("myhost", "1.2.3.4", 22, key) is True

            known_hosts = tmp_path / "known_hosts"
            assert known_hosts.exists()
            assert "user@myhost:22" in known_hosts.read_text()

    def test_matching_stored_key_is_accepted(self, tmp_path):
        (tmp_path / "known_hosts").write_text("user@myhost:22 ssh-ed25519 AAAAmatching\n")
        with patch.object(ssh_connector, "_STATE_DIR", tmp_path):
            client = _TofuClient("myhost", "user@myhost:22")
            with patch("asyncssh.read_known_hosts") as mock_read:
                stored_key = MagicMock()
                stored_key.get_fingerprint.return_value = "SHA256:same"
                mock_read.return_value.match.return_value = ([stored_key],)

                key = MagicMock()
                key.get_fingerprint.return_value = "SHA256:same"

                assert client.validate_host_public_key("myhost", "1.2.3.4", 22, key) is True

    def test_mismatched_stored_key_is_rejected(self, tmp_path):
        (tmp_path / "known_hosts").write_text("user@myhost:22 ssh-ed25519 AAAAoriginal\n")
        with patch.object(ssh_connector, "_STATE_DIR", tmp_path):
            client = _TofuClient("myhost", "user@myhost:22")
            with patch("asyncssh.read_known_hosts") as mock_read:
                stored_key = MagicMock()
                stored_key.get_fingerprint.return_value = "SHA256:original"
                mock_read.return_value.match.return_value = ([stored_key],)

                new_key = MagicMock()
                new_key.get_fingerprint.return_value = "SHA256:different"

                assert client.validate_host_public_key("myhost", "1.2.3.4", 22, new_key) is False


class TestClose:
    def test_close_closes_cached_connection(self):
        conn = SSHConnection("h")
        mock_conn = MagicMock()
        conn._conn = mock_conn
        conn.close()
        mock_conn.close.assert_called_once()
        assert conn._conn is None

    def test_close_with_no_connection_does_not_raise(self):
        conn = SSHConnection("h")
        conn.close()

    def test_close_swallows_errors(self):
        conn = SSHConnection("h")
        mock_conn = MagicMock()
        mock_conn.close.side_effect = Exception("boom")
        conn._conn = mock_conn
        conn.close()


class TestContextManager:
    def test_exit_calls_close(self):
        conn = SSHConnection("h")
        with patch.object(conn, "close") as mock_close:
            with conn:
                pass
            mock_close.assert_called_once()
