import pytest
from unittest.mock import MagicMock, AsyncMock, patch


class _PluginStore:
    """Test-only per-plugin write/read handle. Production no longer has a
    StorageOrchestrator — the engine writes via db.apply_result(target, id,
    name, result) — but tests keep this thin adapter so `plugin.storage.apply`/
    `latest_metric`/`latest_snapshot`/`snapshot` stay a convenient scoped
    surface. Every method just forwards to the identity-parameterized
    DatabaseManager calls the real engine uses."""

    def __init__(self, db, target, plugin_name, plugin_id):
        self._db = db
        self._target = target
        self._name = plugin_name
        self._id = plugin_id

    def apply(self, result):
        self._db.apply_result(self._target, self._id, self._name, result)

    def snapshot(self, rows):
        # Snapshots are stored as decoded objects in the state store and
        # serialised only on the way to disk, so this passes rows through.
        self._db.set_snapshot(self._id, rows)

    def latest_metric(self, metric_name):
        return self._db.latest_metric(self._id, metric_name)

    def latest_snapshot(self, default=None):
        return self._db.latest_snapshot(self._id, default)

    def get_setting(self, key, default=None):
        return self._db.get_setting(key, default)


@pytest.fixture(autouse=True)
def _synchronous_db_writes():
    from vigil.core.database.database import _writer
    prev = _writer.synchronous
    _writer.synchronous = True
    yield
    _writer.synchronous = prev


@pytest.fixture
def db_manager(tmp_path):
    from vigil.core.database.database import DatabaseManager, db
    if not db.is_closed():
        db.close()
    manager = DatabaseManager(str(tmp_path / "test.db"))
    yield manager
    if not db.is_closed():
        db.close()


class _FakeEngine:
    """Stands in for the Coordination Engine in plugin tests. Exposes the
    narrow engine surface pure plugins call back into (apply/set_setting).
    Mirrors VigilEngine's implementations so a plugin behaves identically
    here and in the real app; job control rides plugin.jobs, wired in
    make_plugin the way VigilEngine._wire_plugin does."""

    def __init__(self, db, plugin, connectors=None):
        self.db = db
        self._plugin = plugin
        self.plugins = [plugin]
        self._last_collected = {}
        self.connectors = connectors
        # Tests set http_fetch_handler(request)->HttpResult to drive io_call()
        # closures that issue HTTP through engine.http_fetch (login-then-fetch).
        self.http_fetch_handler = None

    def apply(self, plugin, result):
        self.db.apply_result(plugin.target, plugin.id, plugin.name, result)

    async def http_fetch(self, request):
        if self.http_fetch_handler is None:
            raise AssertionError(
                "engine.http_fetch called but no http_fetch_handler set on the "
                "fake engine — set plugin.engine.http_fetch_handler in the test")
        return self.http_fetch_handler(request)

    def set_setting(self, key, value):
        self.db.set_setting(key, value)


@pytest.fixture
def make_plugin(db_manager):
    def factory(cls, extra_config=None):
        from vigil.core.connectors.engine import ConnectorEngine
        from vigil.core.coordination.data_view import PluginDataView

        cfg = {
            "name": "test-plugin",
            "id":   "test-plugin",
            "interval": 60,
            "ssh_config": {"host": "test.host"},
        }
        if extra_config:
            cfg.update(extra_config)

        # Pure plugin: constructed with only (name, config).
        plugin = cls(cfg["name"], cfg)

        # Engine-owned IO/persistence, built and attached the way
        # VigilEngine._wire_plugin does — but with the SSH transport mocked.
        connectors = ConnectorEngine()
        with patch("vigil.core.connectors.engine.SSHConnection") as MockSSH:
            mock_conn = MagicMock()
            mock_conn.host = cfg.get("ssh_config", {}).get("host", "test.host")
            mock_conn.execute = AsyncMock(return_value=(0, "", ""))
            MockSSH.from_config.return_value = mock_conn
            net = connectors.exec_context(cfg, collect_timeout=plugin.timeout)
        plugin.target = net.target

        plugin.network = net
        plugin.storage = _PluginStore(db_manager, plugin.target, plugin.name, plugin.id)
        plugin.bind(PluginDataView(db_manager, plugin.id))
        plugin.engine = _FakeEngine(db_manager, plugin, connectors=connectors)

        from vigil.core.coordination.jobs import JobsGateway

        async def _cancel_exec(command):
            await connectors.execute_raw(net, command)

        plugin.jobs = JobsGateway(db_manager, plugin, cancel_exec=_cancel_exec)
        return plugin

    return factory


@pytest.fixture
def run_cycle():
    """Drives a Plugin's commands()/parse() through a fake command
    runner and applies the result via plugin.storage, mirroring
    VigilEngine._run_cycle without needing a real ConnectorEngine/event
    loop scheduler. commands()/parse() are pure/synchronous, so no awaiting
    is needed here. `fake_run` maps Command -> CmdResult; defaults to (0, "", "")."""
    from vigil.core.connectors.types import CmdResult

    def factory(plugin, fake_run=None):
        commands = plugin.commands()
        if fake_run is None:
            results = [CmdResult(0, "", "") for _ in commands]
        else:
            results = [fake_run(c) for c in commands]
        result = plugin.parse(results)
        plugin.storage.apply(result)
        return result

    return factory


@pytest.fixture
def run_requests():
    """Drives a Plugin's declarative requests()/parse_results() through a fake
    connector and applies the result, mirroring VigilEngine._run_cycle for
    plugins that talk HTTP/DNS/ICMP. requests()/parse_results() are pure, so no
    event loop or real ConnectorEngine is needed. `fake_run` maps each Request
    to a Result (HttpResult/DnsResult/PingResult); with no requests it drives
    parse_results([]) (the 'nothing to query' case, e.g. dns_record with no
    domain)."""

    def factory(plugin, fake_run=None):
        requests = plugin.requests()
        if fake_run is None:
            results = []
        else:
            results = [fake_run(r) for r in requests]
        result = plugin.parse_results(results)
        plugin.storage.apply(result)
        return result

    return factory


_UNSET = object()


@pytest.fixture
def run_io_cycle():
    """Like run_cycle, but for plugins using the io_call() escape hatch
    (sequential local IO) — invokes io_call()'s closure directly and applies
    the CollectResult from parse_results([result]), mirroring the engine.
    `fake_result`, if given, replaces the closure's return value so a test can
    inject a fabricated sample without running real IO. For a plugin whose
    io_call() closure is async (e.g. HTTP login-then-fetch), use
    run_io_cycle_async instead."""

    def factory(plugin, fake_result=_UNSET):
        if fake_result is _UNSET:
            io_result = plugin.io_call()()
        else:
            io_result = fake_result
        result = plugin.parse_results([io_result])
        plugin.storage.apply(result)
        return result

    return factory


@pytest.fixture
def run_io_cycle_async():
    """run_io_cycle for plugins whose io_call() closure is async — awaits the
    coroutine (mirroring the engine's _run_io, which awaits a coroutine
    function). Used by login-then-fetch HTTP plugins that issue dependent
    requests through engine.http_fetch."""

    async def factory(plugin, fake_result=_UNSET):
        if fake_result is _UNSET:
            io_result = await plugin.io_call()()
        else:
            io_result = fake_result
        result = plugin.parse_results([io_result])
        plugin.storage.apply(result)
        return result

    return factory
