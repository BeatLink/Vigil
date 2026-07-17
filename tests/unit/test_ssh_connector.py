import pytest
from unittest.mock import MagicMock, patch
from vigil.core.common import ssh_connector
from vigil.core.common.ssh_connector import SSHConnection


@pytest.fixture(autouse=True)
def _clear_pool():
    """Isolate the shared connection pool between tests."""
    ssh_connector._connection_pool.clear()
    yield
    ssh_connector._connection_pool.clear()


def _make_paramiko_mock(exit_status=0, stdout_bytes=b"output", stderr_bytes=b""):
    """Builds a minimal paramiko client mock with controllable exec_command output."""
    mock_stdout = MagicMock()
    mock_stdout.channel.recv_exit_status.return_value = exit_status
    mock_stdout.read.return_value = stdout_bytes
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = stderr_bytes

    mock_client = MagicMock()
    mock_client.exec_command.return_value = (None, mock_stdout, mock_stderr)
    return mock_client


class TestConnectionPooling:
    def test_same_target_returns_same_instance(self):
        a = SSHConnection.get_shared("h1", username="u", port=22)
        b = SSHConnection.get_shared("h1", username="u", port=22)
        assert a is b

    def test_different_host_returns_new_instance(self):
        a = SSHConnection.get_shared("h1", username="u")
        b = SSHConnection.get_shared("h2", username="u")
        assert a is not b

    def test_different_user_returns_new_instance(self):
        a = SSHConnection.get_shared("h1", username="u1")
        b = SSHConnection.get_shared("h1", username="u2")
        assert a is not b

    def test_from_config_pools_by_target(self):
        a = SSHConnection.from_config({"ssh_config": {"host": "shared.host", "username": "vigil"}})
        b = SSHConnection.from_config({"ssh_config": {"host": "shared.host", "username": "vigil"}})
        assert a is b


class TestConnectCooldown:
    def test_connect_skipped_during_cooldown(self):
        conn = SSHConnection("deadhost")
        with patch("paramiko.SSHClient") as MockClient:
            MockClient.return_value.connect.side_effect = OSError("unreachable")
            # First attempt actually tries to connect and fails.
            with pytest.raises(OSError):
                conn.connect()
            assert MockClient.return_value.connect.call_count == 1
            # Second attempt within the cooldown must not re-attempt paramiko.
            with pytest.raises(ConnectionError):
                conn.connect()
            assert MockClient.return_value.connect.call_count == 1

    def test_connect_retried_after_cooldown_elapses(self):
        conn = SSHConnection("deadhost")
        conn.CONNECT_COOLDOWN = 0.0  # effectively disable the cooldown window
        with patch("paramiko.SSHClient") as MockClient:
            MockClient.return_value.connect.side_effect = OSError("unreachable")
            with pytest.raises(OSError):
                conn.connect()
            with pytest.raises(OSError):
                conn.connect()
            assert MockClient.return_value.connect.call_count == 2


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


class TestConnect:
    def test_successful_connect_sets_client(self):
        conn = SSHConnection("myhost", username="user")
        with patch("vigil.core.common.ssh_connector.paramiko") as mock_para:
            mock_client = MagicMock()
            mock_para.SSHClient.return_value = mock_client
            mock_para.AutoAddPolicy.return_value = MagicMock()
            conn.connect()
        assert conn.client is mock_client

    def test_connect_is_idempotent(self):
        conn = SSHConnection("myhost")
        existing = MagicMock()
        conn.client = existing
        conn.connect()
        assert conn.client is existing  # not replaced

    def test_failed_connect_clears_client_and_raises(self):
        conn = SSHConnection("badhost")
        with patch("vigil.core.common.ssh_connector.paramiko") as mock_para:
            mock_client = MagicMock()
            mock_client.connect.side_effect = Exception("refused")
            mock_para.SSHClient.return_value = mock_client
            mock_para.AutoAddPolicy.return_value = MagicMock()
            with pytest.raises(Exception, match="refused"):
                conn.connect()
        assert conn.client is None

    def test_uses_key_filename_when_key_path_provided(self):
        conn = SSHConnection("h", key_path="/home/user/.ssh/id_ed25519")
        with patch("vigil.core.common.ssh_connector.paramiko") as mock_para:
            mock_client = MagicMock()
            mock_para.SSHClient.return_value = mock_client
            mock_para.AutoAddPolicy.return_value = MagicMock()
            conn.connect()
        _, call_kwargs = mock_client.connect.call_args
        assert call_kwargs.get("key_filename") == "/home/user/.ssh/id_ed25519"


class TestExecute:
    def test_returns_exit_code_stdout_stderr(self):
        conn = SSHConnection("myhost")
        conn.client = _make_paramiko_mock(exit_status=0, stdout_bytes=b"hello", stderr_bytes=b"")
        rc, out, err = conn.execute("echo hello")
        assert rc == 0
        assert out == "hello"
        assert err == ""

    def test_nonzero_exit_code_returned(self):
        conn = SSHConnection("myhost")
        conn.client = _make_paramiko_mock(exit_status=1, stdout_bytes=b"", stderr_bytes=b"not found")
        rc, out, err = conn.execute("bad_cmd")
        assert rc == 1
        assert err == "not found"

    def test_connects_if_no_client(self):
        conn = SSHConnection("myhost")
        mock_client = _make_paramiko_mock()
        with patch.object(conn, "connect", side_effect=lambda: setattr(conn, "client", mock_client)):
            conn.execute("ls")
            conn.connect.assert_called_once()

    def test_execution_error_clears_client_and_raises(self):
        conn = SSHConnection("myhost")
        mock_client = MagicMock()
        mock_client.exec_command.side_effect = Exception("channel closed")
        conn.client = mock_client
        with pytest.raises(Exception, match="channel closed"):
            conn.execute("cmd")
        assert conn.client is None  # forced reconnect on next call


class TestClose:
    def test_close_calls_client_close(self):
        conn = SSHConnection("myhost")
        mock_client = MagicMock()
        conn.client = mock_client
        conn.close()
        mock_client.close.assert_called_once()
        assert conn.client is None

    def test_close_with_no_client_is_safe(self):
        conn = SSHConnection("myhost")
        conn.close()  # should not raise


class TestContextManager:
    def test_enter_calls_connect(self):
        conn = SSHConnection("myhost")
        with patch.object(conn, "connect") as mock_connect, \
             patch.object(conn, "close"):
            with conn:
                mock_connect.assert_called_once()

    def test_exit_calls_close(self):
        conn = SSHConnection("myhost")
        with patch.object(conn, "connect"), \
             patch.object(conn, "close") as mock_close:
            with conn:
                pass
            mock_close.assert_called_once()
