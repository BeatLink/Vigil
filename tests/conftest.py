import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.fixture(autouse=True)
def _synchronous_db_writes():
    from vigil.core.data.database import _writer
    prev = _writer.synchronous
    _writer.synchronous = True
    yield
    _writer.synchronous = prev


@pytest.fixture
def db_manager(tmp_path):
    from vigil.core.data.database import DatabaseManager, db
    if not db.is_closed():
        db.close()
    manager = DatabaseManager(str(tmp_path / "test.db"))
    yield manager
    if not db.is_closed():
        db.close()


@pytest.fixture
def make_plugin(db_manager):
    def factory(cls, extra_config=None):
        cfg = {
            "name": "test-plugin",
            "id":   "test-plugin",
            "interval": 60,
            "ssh_config": {"host": "test.host"},
        }
        if extra_config:
            cfg.update(extra_config)

        with patch("vigil.collector.plugin_base.SSHConnection") as MockSSH, \
             patch("vigil.collector.plugin_base.SSHCollector") as MockCollector, \
             patch("vigil.collector.plugin_base.SSHController") as MockController:

            mock_conn = MagicMock()
            mock_conn.host = cfg.get("ssh_config", {}).get("host", "test.host")
            MockSSH.from_config.return_value = mock_conn
            MockCollector.return_value = MagicMock(
                fetch_output=AsyncMock(return_value=(0, "", ""))
            )
            MockController.return_value = MagicMock(
                execute_action=AsyncMock(return_value=(0, "", ""))
            )
            plugin = cls(cfg["name"], cfg, db_manager)

        plugin.ssh_collector = MagicMock(
            fetch_output=AsyncMock(return_value=(0, "", ""))
        )
        plugin.ssh_controller = MagicMock(
            execute_action=AsyncMock(return_value=(0, "", ""))
        )
        from vigil.collector.controllers.job_controller import JobController
        mock_ssh = MagicMock()
        mock_ssh.execute_streaming = AsyncMock(return_value=(0, ""))
        plugin.job_controller = JobController(
            mock_ssh, db_manager, cfg["id"], mock_conn.host
        )
        plugin.internal_modules['controllers']['job'] = plugin.job_controller
        return plugin

    return factory
