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


@pytest.fixture
def make_plugin(db_manager):
    def factory(cls, extra_config=None):
        from vigil.core.connectors.orchestration.network_orchestrator import SSHConnectionPool

        cfg = {
            "name": "test-plugin",
            "id":   "test-plugin",
            "interval": 60,
            "ssh_config": {"host": "test.host"},
        }
        if extra_config:
            cfg.update(extra_config)

        with patch("vigil.core.connectors.orchestration.network_orchestrator.SSHConnection") as MockSSH, \
             patch("vigil.core.connectors.orchestration.network_orchestrator.SSHCollector") as MockCollector, \
             patch("vigil.core.connectors.orchestration.network_orchestrator.SSHController") as MockController:

            mock_conn = MagicMock()
            mock_conn.host = cfg.get("ssh_config", {}).get("host", "test.host")
            MockSSH.from_config.return_value = mock_conn
            MockCollector.return_value = MagicMock(
                fetch_output=AsyncMock(return_value=(0, "", ""))
            )
            MockController.return_value = MagicMock(
                execute_action=AsyncMock(return_value=(0, "", ""))
            )
            pool = SSHConnectionPool()
            plugin = cls(cfg["name"], cfg, db_manager, pool)

        plugin.network._collector = MagicMock(
            fetch_output=AsyncMock(return_value=(0, "", ""))
        )
        plugin.network._controller = MagicMock(
            execute_action=AsyncMock(return_value=(0, "", ""))
        )
        from vigil.core.connectors.job_controller import JobController
        mock_ssh = MagicMock()
        mock_ssh.execute_streaming = AsyncMock(return_value=(0, ""))
        plugin.network._job = JobController(
            mock_ssh, db_manager, cfg["id"], mock_conn.host
        )
        return plugin

    return factory


@pytest.fixture
def run_cycle():
    """Drives a Plugin's commands()/parse() through a fake command
    runner and applies the result via StorageOrchestrator, mirroring
    VigilEngine._run_cycle without needing a real NetworkOrchestrator/event
    loop scheduler. commands()/parse() are pure/synchronous, so no awaiting
    is needed here. `fake_run` maps Command -> CmdResult; defaults to (0, "", "")."""
    from vigil.core.connectors.orchestration.types import CmdResult

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
def run_local_cycle():
    """Like run_cycle, but for plugins using local_call()/parse_local()
    (no SSH involved) — invokes the closure returned by local_call()
    directly (synchronously, no thread offload needed in tests) and applies
    the CollectResult from parse_local()."""

    def factory(plugin):
        fn = plugin.local_call()
        local_result = fn()
        result = plugin.parse_local(local_result)
        plugin.storage.apply(result)
        return result

    return factory


@pytest.fixture
def run_local_cycle_async():
    """Like run_local_cycle, but awaits local_call()'s closure — for
    plugins whose local_call() returns an async function (e.g. one wrapping
    asyncio.create_subprocess_exec) rather than a blocking sync callable."""

    async def factory(plugin):
        fn = plugin.local_call()
        local_result = await fn()
        result = plugin.parse_local(local_result)
        plugin.storage.apply(result)
        return result

    return factory
