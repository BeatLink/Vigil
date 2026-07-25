import pytest
from unittest.mock import MagicMock, AsyncMock, patch


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
    """Stands in for the Coordination Engine in plugin tests. Owns the same
    per-plugin StorageOrchestrator the real engine wires, and exposes the
    narrow engine surface pure plugins call back into (apply/set_setting and
    the DB-backed job methods). Mirrors VigilEngine's implementations so a
    plugin behaves identically here and in the real app."""

    def __init__(self, db, plugin, connectors=None):
        self.db = db
        self._plugin = plugin
        self.plugins = [plugin]
        self._last_collected = {}
        self.connectors = connectors

    @property
    def _store(self):
        return {self._plugin.id: self._plugin.storage}

    def apply(self, plugin, result):
        plugin.storage.apply(result)

    def set_setting(self, key, value):
        self.db.set_setting(key, value)

    # --- job surface (DB-backed, same as VigilEngine) ---
    def job_running(self, plugin):
        jobs = self.db.running_jobs(plugin_id=plugin.id)
        return jobs[0] if jobs else None

    def job_is_running(self, plugin):
        return self.job_running(plugin) is not None

    def job_current_id(self, plugin):
        job = self.job_running(plugin)
        return job['id'] if job else None

    def job_recent(self, plugin, limit=20):
        return self.db.recent_jobs(plugin_id=plugin.id, limit=limit)

    async def job_cancel(self, plugin):
        from vigil.core.connectors.ssh.job_controller import cancel_command
        job = self.job_running(plugin)
        if not job or not job.get('pid'):
            return False
        await self.connectors.execute_raw(plugin.network, cancel_command(job['pid']))
        self.db.finish_job(job['id'], 'cancelled', exit_code=130, error='Cancelled by user')
        return True

    def create_job(self, plugin, kind, command, workdir):
        return self.db.create_job(plugin_id=plugin.id, target=plugin.target,
                                  kind=kind, command=command, workdir=workdir)

    def set_job_pid(self, job_id, pid):
        self.db.set_job_pid(job_id, pid)

    def set_job_progress(self, job_id, summary):
        self.db.set_job_progress(job_id, summary)

    def append_job_output(self, job_id, lines):
        self.db.append_job_output(job_id, lines)

    def bump_job_output_seq(self, job_id, new_seq):
        self.db.bump_job_output_seq(job_id, new_seq)

    def finish_job(self, job_id, state, exit_code=None, error=None):
        self.db.finish_job(job_id, state, exit_code=exit_code, error=error)


@pytest.fixture
def make_plugin(db_manager):
    def factory(cls, extra_config=None):
        from vigil.core.connectors.engine import ConnectorEngine
        from vigil.core.database.storage_orchestrator import StorageOrchestrator
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
            net = connectors.ssh_context(cfg, collect_timeout=plugin.timeout)
        plugin.target = net.target

        plugin.network = net
        plugin.storage = StorageOrchestrator(db_manager, plugin.target, plugin.name, plugin.id)
        plugin.bind(PluginDataView(db_manager, plugin.id, plugin.target, plugin.name,
                                   store=plugin.storage))
        plugin.engine = _FakeEngine(db_manager, plugin, connectors=connectors)
        return plugin

    return factory


@pytest.fixture
def run_cycle():
    """Drives a Plugin's commands()/parse() through a fake command
    runner and applies the result via StorageOrchestrator, mirroring
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
    inject a fabricated sample without running real IO."""

    def factory(plugin, fake_result=_UNSET):
        if fake_result is _UNSET:
            io_result = plugin.io_call()()
        else:
            io_result = fake_result
        result = plugin.parse_results([io_result])
        plugin.storage.apply(result)
        return result

    return factory
