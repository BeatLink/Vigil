"""Agent transport: the exec RPC, event routing, and transport selection."""

import asyncio
import pytest
from unittest.mock import MagicMock

from vigil.core.connectors.agent_connector import AgentConnection, AgentRegistry
from vigil.core.connectors.engine import ConnectorEngine, ExecContext
from vigil_agent import protocol as proto


class FakeSocket:
    """Captures the frames the server sends and lets a test answer them."""

    def __init__(self):
        self.sent = []

    async def send_text(self, raw):
        self.sent.append(proto.decode(raw))

    def last(self):
        return self.sent[-1]


def _connected(agent_id="node-a"):
    conn = AgentConnection(agent_id, host="node-a.lan")
    socket = FakeSocket()
    conn.attach(socket, {'hostname': 'node-a.lan', 'version': '0.1.0', 'caps': ['journal']})
    return conn, socket


class TestExecRpc:
    async def test_result_frame_completes_the_call(self):
        conn, socket = _connected()

        call = asyncio.create_task(conn.execute("uptime", timeout=5))
        await asyncio.sleep(0)  # let execute() send its frame

        frame = socket.last()
        assert frame['t'] == proto.EXEC
        assert frame['cmd'] == "uptime"

        conn.resolve(frame['id'], 0, "up 3 days", "")
        assert await call == (0, "up 3 days", "")

    async def test_disconnected_agent_fails_without_raising(self):
        conn = AgentConnection("node-a", host="node-a.lan")
        code, out, err = await conn.execute("uptime")
        assert (code, out) == (-1, "")
        assert "not connected" in err

    async def test_disconnect_fails_calls_in_flight(self):
        conn, socket = _connected()
        call = asyncio.create_task(conn.execute("sleep 60", timeout=30))
        await asyncio.sleep(0)

        conn.detach(socket)

        code, _, err = await call
        assert code == -1
        assert "disconnected" in err

    async def test_a_stale_result_id_is_ignored(self):
        conn, _ = _connected()
        conn.resolve(999, 0, "late", "")  # must not raise

    async def test_commands_are_concurrent_not_serialised(self):
        """The point of the transport: no per-host session ceiling, so a slow
        command cannot delay a fast one on the same connection."""
        conn, socket = _connected()

        calls = [asyncio.create_task(conn.execute(f"cmd{i}", timeout=5)) for i in range(20)]
        await asyncio.sleep(0)

        assert len(socket.sent) == 20
        for frame in socket.sent:
            conn.resolve(frame['id'], 0, frame['cmd'], "")
        assert [r[1] for r in await asyncio.gather(*calls)] == [f"cmd{i}" for i in range(20)]


class TestRegistry:
    def test_configure_builds_connections_and_tokens(self):
        registry = AgentRegistry()
        registry.configure([{'id': 'node-a', 'token': 'secret', 'host': 'node-a.lan'}])

        assert registry.token_for('node-a') == 'secret'
        assert registry.get('node-a').host == 'node-a.lan'

    def test_an_entry_without_a_token_never_authenticates(self):
        registry = AgentRegistry()
        registry.configure([{'id': 'node-a'}])
        assert registry.token_for('node-a') is None

    def test_an_undeclared_agent_yields_a_failing_placeholder(self):
        registry = AgentRegistry()
        conn = registry.require('never-declared')
        assert conn.is_connected is False

    def test_events_reach_the_sink(self):
        registry = AgentRegistry()
        seen = []
        registry.set_event_sink(lambda *args: seen.append(args))
        registry.dispatch_event('node-a', 'stream-1', 123.0, {'message': 'boom'})
        assert seen == [('node-a', 'stream-1', 123.0, {'message': 'boom'})]

    def test_a_raising_sink_does_not_escape(self):
        registry = AgentRegistry()

        def _explode(*_args):
            raise RuntimeError("plugin bug")

        registry.set_event_sink(_explode)
        registry.dispatch_event('node-a', 'stream-1', 123.0, {})  # must not raise


class TestTransportSelection:
    def test_agent_key_selects_the_agent_transport(self):
        engine = ConnectorEngine()
        engine.agents.configure([{'id': 'node-a', 'token': 't', 'host': 'node-a.lan'}])

        ctx = engine.exec_context({'agent': 'node-a'})
        assert ctx.is_agent
        assert ctx.target == 'node-a.lan'

    def test_without_an_agent_key_ssh_is_still_used(self):
        engine = ConnectorEngine()
        ctx = engine.exec_context({'ssh_config': {'host': 'web-01'}})
        assert not ctx.is_agent
        assert ctx.target == 'web-01'

    def test_ssh_context_remains_an_alias(self):
        engine = ConnectorEngine()
        assert engine.ssh_context({'ssh_config': {'host': 'web-01'}}).target == 'web-01'


class TestStreamSpec:
    def test_round_trips_through_the_wire(self):
        spec = proto.StreamSpec('mon-1', 'journal', {'unit': 'nginx.service'})
        assert proto.StreamSpec.from_wire(spec.to_wire()) == spec

    def test_a_spec_without_a_kind_is_rejected(self):
        assert proto.StreamSpec.from_wire({'id': 'mon-1'}) is None

    def test_decode_survives_a_malformed_frame(self):
        assert proto.decode("not json") == {}
        assert proto.decode("[1,2,3]") == {}


class TestAgentExecutor:
    async def test_returns_the_ssh_shaped_triple(self):
        from vigil_agent import executor
        assert await executor.run("echo hello") == (0, "hello\n", "")

    async def test_a_failing_command_reports_its_code_and_stderr(self):
        from vigil_agent import executor
        code, _, err = await executor.run("echo oops >&2; exit 3")
        assert code == 3
        assert "oops" in err

    async def test_a_timeout_kills_the_whole_process_group(self, tmp_path):
        """The gap the SSH transport had: killing the remote shell left its
        children running. Here a grandchild must die with the command."""
        import os
        from vigil_agent import executor

        pid_file = tmp_path / "child.pid"
        code, _, err = await executor.run(
            f"sh -c 'echo $$ > {pid_file}; sleep 30' & sleep 30", timeout=0.5
        )
        assert code == -1
        assert "Timed out" in err

        child_pid = int(pid_file.read_text().strip())
        await asyncio.sleep(0.2)  # let the group kill land
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)


class TestEventRouting:
    """The engine glue between an inbound frame and a plugin's parse_event."""

    @staticmethod
    def _engine(db, plugin):
        # Built field-by-field rather than through __init__, which would stand
        # up the config loader, exporters and connectors this path never uses.
        from vigil.core.coordination.engine import VigilEngine
        engine = object.__new__(VigilEngine)
        engine.db = db
        engine._event_targets = {plugin.id: plugin}
        return engine

    def test_an_event_is_parsed_and_persisted(self, make_plugin, db_manager):
        from vigil.plugins.oom import Oom
        plugin = make_plugin(Oom)
        engine = self._engine(db_manager, plugin)

        engine._on_agent_event('node-a', plugin.id, 1234.0,
                               {'message': 'Out of memory: Killed process 42 (redis)'})

        messages = [row['message'] for row in db_manager.recent_events(limit=10)]
        assert any('redis' in m for m in messages)

    def test_an_event_for_an_unknown_stream_is_dropped(self, make_plugin, db_manager):
        from vigil.plugins.oom import Oom
        engine = self._engine(db_manager, make_plugin(Oom))
        engine._on_agent_event('node-a', 'no-such-stream', 1234.0, {})  # must not raise

    def test_a_plugin_that_raises_does_not_kill_the_socket(self, make_plugin, db_manager):
        from vigil.plugins.oom import Oom
        plugin = make_plugin(Oom)
        engine = self._engine(db_manager, plugin)
        plugin.parse_event = MagicMock(side_effect=RuntimeError("plugin bug"))

        engine._on_agent_event('node-a', plugin.id, 1234.0, {'message': 'x'})


class TestPluginSubscriptions:
    def test_oom_subscribes_to_the_kernel_journal(self, make_plugin):
        from vigil.plugins.oom import Oom
        plugin = make_plugin(Oom)
        spec = plugin.subscriptions()[0]
        assert (spec.id, spec.kind) == (plugin.id, 'journal')
        assert spec.params['kernel'] is True

    def test_systemd_subscribes_to_its_own_unit(self, make_plugin):
        from vigil.plugins.systemd_service import SystemdService
        plugin = make_plugin(SystemdService, {'service_name': 'nginx.service'})
        assert plugin.subscriptions()[0].params['unit'] == 'nginx.service'

    def test_a_streamed_line_never_changes_status(self, make_plugin):
        """Status stays owned by the poll, so one noisy log line cannot flip a
        healthy service to failed."""
        from vigil.plugins.systemd_service import SystemdService
        plugin = make_plugin(SystemdService, {'service_name': 'nginx.service'})
        result = plugin.parse_event(plugin.id, {'message': 'FAILED to reload'}, 1234.0)
        assert result.status is None
        assert result.log_lines[0][1] == 'ERROR'

    def test_a_poll_only_plugin_declares_no_streams(self, make_plugin):
        from vigil.plugins.uptime import Uptime
        assert make_plugin(Uptime).subscriptions() == []
