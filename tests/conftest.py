"""
Root-level fixtures shared across all test modules.

Key design decisions:
- Uses a file-based SQLite temp DB (not :memory:) so that Peewee's
  connection_context() pattern works correctly across multiple open/close cycles.
- SSHConnection, SSHCollector, and SSHController are patched during plugin __init__
  so plugins instantiate cleanly without real network access.
- After instantiation, ssh_collector / ssh_controller are replaced with fresh
  MagicMocks so each test can configure return values independently.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.fixture(autouse=True)
def _synchronous_db_writes():
    """
    Run DB writes inline during tests so a write is immediately visible to the
    next read. In production writes are queued to a background thread (to keep
    fsync off the event loop); tests don't want that indirection.
    """
    from vigil.core.data.database import _writer
    prev = _writer.synchronous
    _writer.synchronous = True
    yield
    _writer.synchronous = prev


@pytest.fixture
def db_manager(tmp_path):
    """DatabaseManager backed by a temp SQLite file. Cleans up on teardown."""
    from vigil.core.data.database import DatabaseManager, db
    if not db.is_closed():
        db.close()
    manager = DatabaseManager(str(tmp_path / "test.db"))
    yield manager
    if not db.is_closed():
        db.close()


@pytest.fixture
def make_plugin(db_manager):
    """
    Factory fixture: creates any BasePlugin subclass with all external deps mocked.
    Returns a callable: make_plugin(PluginClass, extra_config_dict).
    The plugin's ssh_collector and ssh_controller are fresh AsyncMocks for per-test control.
    """
    def factory(cls, extra_config=None):
        cfg = {
            "name": "test-plugin",
            "id":   "test-plugin",
            "interval": 60,
            "ssh_config": {"host": "test.host"},
        }
        if extra_config:
            cfg.update(extra_config)

        with patch("vigil.core.common.base_plugin.SSHConnection") as MockSSH, \
             patch("vigil.core.common.base_plugin.SSHCollector") as MockCollector, \
             patch("vigil.core.common.base_plugin.SSHController") as MockController:

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

        # Replace with fresh mocks that each test controls directly
        plugin.ssh_collector = MagicMock(
            fetch_output=AsyncMock(return_value=(0, "", ""))
        )
        plugin.ssh_controller = MagicMock(
            execute_action=AsyncMock(return_value=(0, "", ""))
        )
        # The job controller is kept real (backed by the temp DB) so job
        # lifecycle and persistence are genuinely exercised; only the SSH
        # connection under it is mocked. Tests drive it by setting
        # plugin.job_controller.ssh.execute_streaming.
        from vigil.core.modules.controllers.job_controller import JobController
        mock_ssh = MagicMock()
        mock_ssh.execute_streaming = MagicMock(return_value=(0, ""))
        plugin.job_controller = JobController(
            mock_ssh, db_manager, cfg["id"], mock_conn.host
        )
        plugin.internal_modules['controllers']['job'] = plugin.job_controller
        return plugin

    return factory
