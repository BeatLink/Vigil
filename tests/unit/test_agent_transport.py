"""Agent transport: the exec RPC, event routing, and transport selection."""

import asyncio
import contextlib
import json
import pytest
from unittest.mock import MagicMock

from vigil.core.connectors.agent_connector import AgentConnection, AgentRegistry
from vigil.core.connectors.engine import ConnectorEngine
from vigil_agent import protocol as proto
from vigil.core.connectors.types import CmdResult


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
        engine._event_targets.update({s.id: plugin for s in plugin.subscriptions()})
        return engine

    def test_an_event_is_parsed_and_persisted(self, make_plugin, db_manager):
        from vigil.plugins.oom import Oom
        plugin = make_plugin(Oom, {})
        engine = self._engine(db_manager, plugin)

        engine._on_agent_event('node-a', f'{plugin.id}:journal', 1234.0,
                               {'message': 'Out of memory: Killed process 42 (redis)'})

        messages = [row['message'] for row in db_manager.recent_events(limit=10)]
        assert any('redis' in m for m in messages)

    def test_an_event_for_an_unknown_stream_is_dropped(self, make_plugin, db_manager):
        from vigil.plugins.oom import Oom
        engine = self._engine(db_manager, make_plugin(Oom, {}))
        engine._on_agent_event('node-a', 'no-such-stream', 1234.0, {})  # must not raise

    def test_a_plugin_that_raises_does_not_kill_the_socket(self, make_plugin, db_manager):
        from vigil.plugins.oom import Oom
        plugin = make_plugin(Oom, {})
        engine = self._engine(db_manager, plugin)
        plugin.parse_event = MagicMock(side_effect=RuntimeError("plugin bug"))

        engine._on_agent_event('node-a', plugin.id, 1234.0, {'message': 'x'})


class TestPluginSubscriptions:
    def test_oom_subscribes_to_the_kernel_journal(self, make_plugin):
        from vigil.plugins.oom import Oom
        plugin = make_plugin(Oom, {})
        spec = next(s for s in plugin.subscriptions() if s.kind == 'journal')
        assert spec.id == f'{plugin.id}:journal'
        assert spec.params['kernel'] is True

    def test_a_sampled_plugin_carries_its_poll_command(self, make_plugin):
        from vigil.plugins.cpu import Cpu
        plugin = make_plugin(Cpu, {})
        spec = next(s for s in plugin.subscriptions() if s.kind == 'sample')
        assert spec.params['command'] == plugin.commands()[0].text
        assert spec.params['interval'] == plugin.interval

    def test_a_sample_frame_parses_through_the_poll_parser(self, make_plugin):
        from vigil.plugins.cpu import Cpu
        plugin = make_plugin(Cpu, {})
        stdout = 'cpu  100 0 100 800 0 0 0 0\ncpu  200 0 200 1200 0 0 0 0'
        result = plugin.parse_event(f'{plugin.id}:sample',
                                    {'exit_code': 0, 'stdout': stdout, 'stderr': ''}, 0.0)
        assert result.metrics == plugin.parse([CmdResult(0, stdout, '')]).metrics

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



class TestSampleStreamContract:
    """The generic sample path: a plugin's poll command, run by the agent."""

    def test_a_plugin_is_poll_only_by_default(self, make_plugin):
        from vigil.plugins.borg import Borg
        plugin = make_plugin(Borg, {'repo': '/srv/repo'})
        assert plugin.SAMPLED is False
        assert plugin.event_driven() is False

    def test_no_stream_without_exactly_one_command(self, make_plugin):
        """service_list issues two commands, so the generic path cannot carry
        it and must not claim to — suppressing its poll would collect nothing."""
        from vigil.plugins.service_list import ServiceList
        plugin = make_plugin(ServiceList, {})
        plugin.SAMPLED = True
        assert plugin.sample_streams() == []
        assert plugin.event_driven() is False

    def test_a_failing_sample_parses_as_a_failure(self, make_plugin):
        from vigil.plugins.cpu import Cpu
        plugin = make_plugin(Cpu, {})
        result = plugin.parse_event(f'{plugin.id}:sample',
                                    {'exit_code': 1, 'stdout': '', 'stderr': 'boom'}, 0.0)
        assert result.status == 'failed'

    def test_a_malformed_frame_does_not_raise(self, make_plugin):
        from vigil.plugins.cpu import Cpu
        plugin = make_plugin(Cpu, {})
        assert plugin.parse_event(f'{plugin.id}:sample', {}, 0.0).status == 'failed'

    def test_the_sample_stream_bounds_quiet_suppression(self, make_plugin):
        """The agent may skip unchanged frames but must push within five
        intervals, so a quiet monitor's stored data keeps advancing."""
        from vigil.plugins.cpu import Cpu
        plugin = make_plugin(Cpu, {})
        spec = plugin.sample_streams()[0]
        assert spec.params['max_quiet'] == plugin.interval * 5


class _Exhausted(Exception):
    """Raised by the fake executor when its scripted outputs run out."""


class TestSampleWatcher:
    """The agent-side sample loop: unchanged-output suppression and the
    max_quiet keepalive that stops suppression from reading as staleness."""

    @staticmethod
    async def _run(params, outputs, monkeypatch):
        from vigil_agent import executor, watchers

        feed = iter(outputs)

        async def fake_run(command, timeout=None):
            try:
                return next(feed)
            except StopIteration:
                raise _Exhausted

        monkeypatch.setattr(executor, 'run', fake_run)
        emitted = []

        async def emit(payload):
            emitted.append(payload)

        with pytest.raises(_Exhausted):
            await watchers.sample({'command': 'c', 'interval': 0, **params}, emit)
        return emitted

    async def test_max_quiet_suppresses_unchanged_output(self, monkeypatch):
        emitted = await self._run({'max_quiet': 999}, [(0, 'same', '')] * 5, monkeypatch)
        assert len(emitted) == 1

    async def test_the_keepalive_still_pushes_an_unchanged_result(self, monkeypatch):
        emitted = await self._run({'max_quiet': 0}, [(0, 'same', '')] * 5, monkeypatch)
        assert len(emitted) == 5

    async def test_an_exit_code_change_alone_is_pushed(self, monkeypatch):
        emitted = await self._run({'max_quiet': 999},
                                  [(0, 'x', ''), (1, 'x', '')], monkeypatch)
        assert [p['exit_code'] for p in emitted] == [0, 1]

    async def test_without_max_quiet_every_result_is_pushed(self, monkeypatch):
        """The compatibility default: an older server that never sends
        max_quiet keeps getting one frame per interval."""
        emitted = await self._run({}, [(0, 'same', '')] * 3, monkeypatch)
        assert len(emitted) == 3

    async def test_on_change_alone_still_fully_suppresses(self, monkeypatch):
        emitted = await self._run({'on_change': True}, [(0, 'same', '')] * 3, monkeypatch)
        assert len(emitted) == 1


class TestJournalMux:
    """Shared journalctl followers: streams with unit filters ride one
    process, and each JSON line is routed to the streams it matches."""

    class _FakeProc:
        def __init__(self, lines, eof=False):
            self._lines = list(lines)
            self._eof = eof
            self.returncode = None
            self.stdout = self

        async def readline(self):
            if self._lines:
                return self._lines.pop(0)
            if self._eof:
                return b''
            await asyncio.Event().wait()

        def terminate(self):
            self.returncode = -15

    @staticmethod
    def _entry(message, **fields):
        return (json.dumps({'MESSAGE': message, **fields}) + '\n').encode()

    @staticmethod
    def _collector(sink, name):
        async def emit(payload):
            sink[name].append(payload['message'])
        return emit

    @staticmethod
    async def _settle(condition, timeout=5.0):
        for _ in range(int(timeout / 0.01)):
            if condition():
                return
            await asyncio.sleep(0.01)
        pytest.fail("the mux never delivered the expected entries")

    @staticmethod
    async def _cancel(tasks):
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def test_per_stream_filters_reproduce_journalctl(self):
        from vigil_agent.watchers import _entry_matches as m
        assert m({'unit': 'redis'}, {'_SYSTEMD_UNIT': 'redis.service'}, 'x')
        assert not m({'unit': 'redis'}, {'_SYSTEMD_UNIT': 'nginx.service'}, 'x')
        assert m({'unit': 'a.service'}, {'UNIT': 'a.service'}, 'x')
        assert m({'priority': 3}, {'PRIORITY': '2'}, 'x')
        assert not m({'priority': 3}, {'PRIORITY': '6'}, 'x')
        assert not m({'priority': 3}, {}, 'x')
        assert m({'grep': 'boom'}, {}, 'kaboom')
        assert not m({'grep': 'boom'}, {}, 'quiet')
        assert m({'identifier': 'sshd'}, {'SYSLOG_IDENTIFIER': 'sshd'}, 'x')
        assert not m({'identifier': 'sshd'}, {'SYSLOG_IDENTIFIER': 'cron'}, 'x')

    async def test_unit_streams_share_one_follower(self, monkeypatch):
        from vigil_agent import watchers
        monkeypatch.setattr(watchers, '_JOURNAL_SETTLE_SECONDS', 0.01)

        spawned = []
        lines = [self._entry('from nginx', _SYSTEMD_UNIT='nginx.service'),
                 self._entry('from redis', _SYSTEMD_UNIT='redis.service'),
                 self._entry('from other', _SYSTEMD_UNIT='other.service')]

        async def fake_spawn(group, cmd):
            spawned.append(cmd)
            return TestJournalMux._FakeProc(lines)

        monkeypatch.setattr(watchers._JournalGroup, '_spawn', fake_spawn)

        mux = watchers.JournalMux()
        got = {'nginx': [], 'redis': []}
        tasks = [
            asyncio.create_task(mux.follow({'unit': 'nginx.service'},
                                           self._collector(got, 'nginx'))),
            asyncio.create_task(mux.follow({'unit': 'redis'},
                                           self._collector(got, 'redis'))),
        ]
        await self._settle(lambda: got['nginx'] and got['redis'])
        await asyncio.sleep(0.05)
        await self._cancel(tasks)

        assert len(spawned) == 1
        assert spawned[0].count('--unit') == 2
        assert 'nginx.service' in spawned[0] and 'redis' in spawned[0]
        assert got == {'nginx': ['from nginx'], 'redis': ['from redis']}

    async def test_a_kernel_stream_keeps_its_own_follower(self, monkeypatch):
        from vigil_agent import watchers
        monkeypatch.setattr(watchers, '_JOURNAL_SETTLE_SECONDS', 0.01)

        spawned = []
        unit_lines = [self._entry('unit line', _SYSTEMD_UNIT='nginx.service')]
        kernel_lines = [self._entry('benign kernel chatter'),
                        self._entry('Out of memory: Killed process 42 (redis)')]

        async def fake_spawn(group, cmd):
            spawned.append(cmd)
            lines = kernel_lines if '--dmesg' in cmd else unit_lines
            return TestJournalMux._FakeProc(lines)

        monkeypatch.setattr(watchers._JournalGroup, '_spawn', fake_spawn)

        mux = watchers.JournalMux()
        got = {'unit': [], 'kernel': []}
        tasks = [
            asyncio.create_task(mux.follow({'unit': 'nginx.service'},
                                           self._collector(got, 'unit'))),
            asyncio.create_task(mux.follow({'kernel': True, 'grep': 'Out of memory'},
                                           self._collector(got, 'kernel'))),
        ]
        await self._settle(lambda: got['unit'] and got['kernel'])
        await self._cancel(tasks)

        assert len(spawned) == 2
        assert got['unit'] == ['unit line']
        assert got['kernel'] == ['Out of memory: Killed process 42 (redis)']

    async def test_a_dead_follower_fails_its_streams(self, monkeypatch):
        """EOF must surface as a raise so supervise() restarts the stream."""
        from vigil_agent import watchers
        monkeypatch.setattr(watchers, '_JOURNAL_SETTLE_SECONDS', 0.01)

        async def fake_spawn(group, cmd):
            return TestJournalMux._FakeProc([], eof=True)

        monkeypatch.setattr(watchers._JournalGroup, '_spawn', fake_spawn)

        mux = watchers.JournalMux()

        async def emit(payload):
            pass

        task = asyncio.create_task(mux.follow({'unit': 'nginx.service'}, emit))
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(task, timeout=5)
