import pytest
from unittest.mock import MagicMock, AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.collector.ssh_runner import SSHCollector, SSHController


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.host = "test.host"
    conn.execute = AsyncMock(return_value=(0, "output", ""))
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

    async def test_exception_returns_minus_one(self, mock_conn):
        mock_conn.execute.side_effect = Exception("connection reset")
        collector = SSHCollector(mock_conn)
        rc, out, err = await collector.fetch_output("cmd")
        assert rc == -1
        assert "connection reset" in err

    async def test_passes_command_to_ssh_execute(self, mock_conn):
        from vigil.collector.ssh_runner import TIMEOUT
        collector = SSHCollector(mock_conn)
        await collector.fetch_output("df -h")
        mock_conn.execute.assert_called_once_with("df -h", timeout=TIMEOUT)

    async def test_collector_timeout_is_configurable(self, mock_conn):
        collector = SSHCollector(mock_conn, timeout=120.0)
        await collector.fetch_output("slow-command")
        mock_conn.execute.assert_called_once_with("slow-command", timeout=120.0)

    async def test_per_call_timeout_overrides_default(self, mock_conn):
        collector = SSHCollector(mock_conn, timeout=30.0)
        await collector.fetch_output("slow-command", timeout=90.0)
        mock_conn.execute.assert_called_once_with("slow-command", timeout=90.0)


class TestExecuteAction:
    async def test_success_returns_tuple(self, mock_conn):
        controller = SSHController(mock_conn)
        rc, out, err = await controller.execute_action("systemctl restart foo")
        assert rc == 0
        assert out == "output"
        assert err == ""

    async def test_nonzero_exit_propagated(self, mock_conn):
        mock_conn.execute.return_value = (1, "", "command not found")
        controller = SSHController(mock_conn)
        rc, out, err = await controller.execute_action("bad_cmd")
        assert rc == 1
        assert err == "command not found"

    async def test_exception_returns_minus_one(self, mock_conn):
        mock_conn.execute.side_effect = Exception("connection reset")
        controller = SSHController(mock_conn)
        rc, out, err = await controller.execute_action("cmd")
        assert rc == -1
        assert "connection reset" in err

    async def test_default_timeout_is_longer_than_collector(self, mock_conn):
        from vigil.collector.ssh_runner import COLLECT_TIMEOUT, CONTROL_TIMEOUT
        controller = SSHController(mock_conn)
        await controller.execute_action("df -h")
        mock_conn.execute.assert_called_once_with("df -h", timeout=CONTROL_TIMEOUT)
        assert CONTROL_TIMEOUT > COLLECT_TIMEOUT

    async def test_controller_timeout_is_configurable(self, mock_conn):
        controller = SSHController(mock_conn, timeout=120.0)
        await controller.execute_action("slow-command")
        mock_conn.execute.assert_called_once_with("slow-command", timeout=120.0)

    async def test_per_call_timeout_overrides_default(self, mock_conn):
        controller = SSHController(mock_conn, timeout=30.0)
        await controller.execute_action("slow-command", timeout=90.0)
        mock_conn.execute.assert_called_once_with("slow-command", timeout=90.0)
