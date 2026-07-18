import asyncio
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.asyncio
from vigil.core.modules.collectors.ssh_collector import SSHCollector


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.host = "test.host"
    conn.execute = MagicMock(return_value=(0, "output", ""))
    return conn


class TestFetchOutput:
    async def test_success_returns_tuple(self, mock_conn):
        collector = SSHCollector(mock_conn)
        rc, out, err = await collector.fetch_output("ls")
        assert rc == 0
        assert out == "output"
        assert err == ""

    async def test_nonzero_exit_propagated(self, mock_conn):
        mock_conn.execute.return_value = (1, "", "command not found")
        collector = SSHCollector(mock_conn)
        rc, out, err = await collector.fetch_output("bad_cmd")
        assert rc == 1
        assert err == "command not found"

    async def test_timeout_returns_minus_one(self, mock_conn):
        # Patch asyncio.wait_for to simulate a timeout without actually waiting
        with patch("vigil.core.modules.collectors.ssh_collector.asyncio.wait_for",
                   side_effect=asyncio.TimeoutError):
            collector = SSHCollector(mock_conn)
            rc, out, err = await collector.fetch_output("slow_cmd")
        assert rc == -1
        assert "Timed out" in err

    async def test_exception_returns_minus_one(self, mock_conn):
        mock_conn.execute.side_effect = Exception("connection reset")
        collector = SSHCollector(mock_conn)
        rc, out, err = await collector.fetch_output("cmd")
        assert rc == -1
        assert "connection reset" in err

    async def test_passes_command_to_ssh_execute(self, mock_conn):
        from vigil.core.modules.collectors.ssh_collector import TIMEOUT
        collector = SSHCollector(mock_conn)
        await collector.fetch_output("df -h")
        # The deadline goes down to execute(), which is what actually kills the
        # process group; wait_for alone would leave it running.
        mock_conn.execute.assert_called_once_with("df -h", timeout=TIMEOUT)

    async def test_collector_timeout_is_configurable(self, mock_conn):
        collector = SSHCollector(mock_conn, timeout=120.0)
        await collector.fetch_output("slow-command")
        mock_conn.execute.assert_called_once_with("slow-command", timeout=120.0)

    async def test_per_call_timeout_overrides_default(self, mock_conn):
        collector = SSHCollector(mock_conn, timeout=30.0)
        await collector.fetch_output("slow-command", timeout=90.0)
        mock_conn.execute.assert_called_once_with("slow-command", timeout=90.0)
