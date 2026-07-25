import pytest
from unittest.mock import MagicMock, AsyncMock, patch

pytestmark = pytest.mark.asyncio

from vigil.core.connectors.ssh_connector import COLLECT_TIMEOUT, CONTROL_TIMEOUT
from vigil.core.connectors.engine import ConnectorEngine, SSHContext
from vigil.core.connectors.types import ActionPlan, Command


def _make(mock_conn, timeout=COLLECT_TIMEOUT):
    """A ConnectorEngine plus an SSHContext wrapping the mocked connection.
    Patches SSHConnection so ssh_context() pools the mock instead of dialling."""
    engine = ConnectorEngine()
    with patch("vigil.core.connectors.engine.SSHConnection") as MockSSH:
        MockSSH.from_config.return_value = mock_conn
        ctx = engine.ssh_context({"ssh_config": {"host": "test.host"}},
                                 collect_timeout=timeout)
    return engine, ctx


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.host = "test.host"
    conn.execute = AsyncMock(return_value=(0, "output", ""))
    return conn


class TestSSHContext:
    async def test_target_is_connection_host(self, mock_conn):
        _, ctx = _make(mock_conn)
        assert ctx.target == "test.host"

    async def test_context_is_pooled_per_target(self, mock_conn):
        engine = ConnectorEngine()
        with patch("vigil.core.connectors.engine.SSHConnection") as MockSSH:
            MockSSH.from_config.return_value = mock_conn
            cfg = {"ssh_config": {"host": "test.host"}}
            a = engine.ssh_context(cfg)
            b = engine.ssh_context(cfg)
        assert a.conn is b.conn
        MockSSH.from_config.assert_called_once()


class TestCollectCommands:
    async def test_success_returns_cmdresult(self, mock_conn):
        engine, ctx = _make(mock_conn)
        [res] = await engine.run(ctx, [Command("ls")])
        assert (res.exit_code, res.stdout, res.stderr) == (0, "output", "")

    async def test_nonzero_exit_propagated(self, mock_conn):
        mock_conn.execute.return_value = (1, "", "command not found")
        engine, ctx = _make(mock_conn)
        [res] = await engine.run(ctx, [Command("bad_cmd")])
        assert res.exit_code == 1
        assert res.stderr == "command not found"

    async def test_error_tuple_propagated(self, mock_conn):
        # SSHConnection.execute already maps failures to (-1, "", err).
        mock_conn.execute.return_value = (-1, "", "connection reset")
        engine, ctx = _make(mock_conn)
        [res] = await engine.run(ctx, [Command("cmd")])
        assert res.exit_code == -1
        assert "connection reset" in res.stderr

    async def test_default_timeout_is_collect(self, mock_conn):
        engine, ctx = _make(mock_conn)
        await engine.run(ctx, [Command("df -h")])
        mock_conn.execute.assert_called_once_with("df -h", timeout=COLLECT_TIMEOUT)

    async def test_collect_timeout_is_configurable(self, mock_conn):
        engine, ctx = _make(mock_conn, timeout=120.0)
        await engine.run(ctx, [Command("slow-command")])
        mock_conn.execute.assert_called_once_with("slow-command", timeout=120.0)

    async def test_per_command_timeout_overrides_default(self, mock_conn):
        engine, ctx = _make(mock_conn, timeout=30.0)
        await engine.run(ctx, [Command("slow-command", timeout=90.0)])
        mock_conn.execute.assert_called_once_with("slow-command", timeout=90.0)


class TestActionCommands:
    async def test_action_command_uses_control_timeout(self, mock_conn):
        engine, ctx = _make(mock_conn)
        await engine.run(ctx, [Command("systemctl restart foo", action=True)])
        mock_conn.execute.assert_called_once_with(
            "systemctl restart foo", timeout=CONTROL_TIMEOUT
        )

    async def test_control_timeout_longer_than_collect(self):
        assert CONTROL_TIMEOUT > COLLECT_TIMEOUT

    async def test_execute_plan_defaults_to_control_timeout(self, mock_conn):
        engine, ctx = _make(mock_conn)
        res = await engine.execute(ctx, ActionPlan("df -h"))
        assert res.exit_code == 0
        mock_conn.execute.assert_called_once_with("df -h", timeout=CONTROL_TIMEOUT)

    async def test_execute_plan_per_call_timeout_overrides(self, mock_conn):
        engine, ctx = _make(mock_conn)
        await engine.execute(ctx, ActionPlan("slow-command", timeout=90.0))
        mock_conn.execute.assert_called_once_with("slow-command", timeout=90.0)

    async def test_execute_raw_uses_control_timeout(self, mock_conn):
        engine, ctx = _make(mock_conn)
        await engine.execute_raw(ctx, "kill 1234")
        mock_conn.execute.assert_called_once_with("kill 1234", timeout=CONTROL_TIMEOUT)
