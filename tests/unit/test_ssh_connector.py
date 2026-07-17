import subprocess
import pytest
from unittest.mock import patch, MagicMock
from vigil.core.common.ssh_connector import SSHConnection


def _completed(returncode=0, stdout=b"", stderr=b""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


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
        # No pooling anymore — OpenSSH multiplexing handles sharing.
        a = SSHConnection.from_config({"ssh_config": {"host": "h", "username": "u"}})
        b = SSHConnection.from_config({"ssh_config": {"host": "h", "username": "u"}})
        assert a is not b


class TestExecute:
    def test_returns_exit_code_stdout_stderr(self):
        conn = SSHConnection("myhost", username="user")
        with patch("subprocess.run", return_value=_completed(0, b"hello\n", b"")) as run:
            rc, out, err = conn.execute("echo hello")
        assert (rc, out, err) == (0, "hello", "")
        # Target is user@host and the command is the last argv element.
        argv = run.call_args[0][0]
        assert argv[-2] == "user@myhost"
        assert argv[-1] == "echo hello"

    def test_nonzero_exit_code_returned(self):
        conn = SSHConnection("myhost")
        with patch("subprocess.run", return_value=_completed(1, b"", b"not found")):
            rc, out, err = conn.execute("bad_cmd")
        assert rc == 1
        assert err == "not found"

    def test_target_without_username_is_bare_host(self):
        conn = SSHConnection("myhost")  # no username
        with patch("subprocess.run", return_value=_completed(0)) as run:
            conn.execute("ls")
        assert run.call_args[0][0][-2] == "myhost"

    def test_timeout_returns_sentinel(self):
        conn = SSHConnection("slowhost")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=30)):
            rc, out, err = conn.execute("sleep 999", timeout=30)
        assert rc == -1
        assert "Timed out" in err

    def test_generic_error_returns_sentinel(self):
        conn = SSHConnection("h")
        with patch("subprocess.run", side_effect=OSError("ssh not found")):
            rc, out, err = conn.execute("cmd")
        assert rc == -1
        assert "ssh not found" in err

    def test_passes_timeout_to_subprocess(self):
        conn = SSHConnection("h")
        with patch("subprocess.run", return_value=_completed(0)) as run:
            conn.execute("cmd", timeout=12.5)
        assert run.call_args.kwargs["timeout"] == 12.5


class TestMultiplexingOptions:
    def _argv(self, **kwargs):
        conn = SSHConnection("h", username="u", **kwargs)
        with patch("subprocess.run", return_value=_completed(0)) as run:
            conn.execute("cmd")
        return run.call_args[0][0]

    def test_control_master_enabled(self):
        argv = self._argv()
        assert "ControlMaster=auto" in argv

    def test_control_persist_set(self):
        argv = self._argv()
        assert any(a.startswith("ControlPersist=") for a in argv)

    def test_control_path_present(self):
        argv = self._argv()
        assert any(a.startswith("ControlPath=") for a in argv)

    def test_batch_mode_non_interactive(self):
        argv = self._argv()
        assert "BatchMode=yes" in argv

    def test_key_path_passed_with_identities_only(self):
        argv = self._argv(key_path="/run/secrets/vigil_key")
        assert "IdentitiesOnly=yes" in argv
        assert "/run/secrets/vigil_key" in argv

    def test_port_flag_present(self):
        conn = SSHConnection("h", username="u", port=2222)
        with patch("subprocess.run", return_value=_completed(0)) as run:
            conn.execute("cmd")
        argv = run.call_args[0][0]
        assert "-p" in argv and "2222" in argv


class TestControlPath:
    def test_distinct_per_target(self):
        a = SSHConnection("hostA", username="u").execute
        # Compute control paths directly.
        p1 = SSHConnection("hostA", username="u")._control_path()
        p2 = SSHConnection("hostB", username="u")._control_path()
        p3 = SSHConnection("hostA", username="v")._control_path()
        assert p1 != p2
        assert p1 != p3

    def test_same_target_same_path(self):
        p1 = SSHConnection("h", username="u", port=22)._control_path()
        p2 = SSHConnection("h", username="u", port=22)._control_path()
        assert p1 == p2


class TestClose:
    def test_close_sends_control_exit(self):
        conn = SSHConnection("h", username="u")
        with patch("subprocess.run", return_value=_completed(0)) as run:
            conn.close()
        argv = run.call_args[0][0]
        assert "-O" in argv and "exit" in argv

    def test_close_swallows_errors(self):
        conn = SSHConnection("h")
        with patch("subprocess.run", side_effect=OSError("boom")):
            conn.close()  # must not raise


class TestContextManager:
    def test_exit_calls_close(self):
        conn = SSHConnection("h")
        with patch.object(conn, "close") as mock_close:
            with conn:
                pass
            mock_close.assert_called_once()
