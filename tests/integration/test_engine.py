"""
Integration tests for VigilEngine.

These tests exercise real plugin loading via importlib and a real temp-file database,
but mock SSH connections so no actual network calls are made.
"""
import asyncio
import pytest
import yaml
from unittest.mock import MagicMock, AsyncMock, patch
from vigil.collector.main import VigilEngine
from vigil.core.data.database import db


def _write_config(tmp_path, content: dict) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(content))
    return str(path)


@pytest.fixture(autouse=True)
def close_db():
    yield
    if not db.is_closed():
        db.close()


class TestEngineInitialization:
    def test_initializes_with_valid_config(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        cfg_path = _write_config(tmp_path, {
            "database": {"path": db_path},
            "plugins": [],
        })
        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path)
        assert engine.db is not None

    def test_db_path_taken_from_config(self, tmp_path):
        db_path = str(tmp_path / "custom.db")
        cfg_path = _write_config(tmp_path, {"database": {"path": db_path}, "plugins": []})
        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path)
        assert engine.db_path == db_path

    def test_db_path_override_takes_precedence(self, tmp_path):
        cfg_path = _write_config(tmp_path, {"database": {"path": "original.db"}, "plugins": []})
        override = str(tmp_path / "override.db")
        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path, db_path_override=override)
        assert engine.db_path == override


class TestPluginLoading:
    def test_loads_uptime_plugin(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        cfg_path = _write_config(tmp_path, {
            "database": {"path": db_path},
            "plugins": [{
                "name": "My Host",
                "id":   "my-host",
                "type": "uptime",
                "target_host": "127.0.0.1",
            }],
        })
        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path)
            engine.setup_modules()
        assert len(engine.plugins) == 1
        assert engine.plugins[0].name == "My Host"

    def test_unknown_plugin_type_is_skipped(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        cfg_path = _write_config(tmp_path, {
            "database": {"path": db_path},
            "plugins": [{"name": "Bad", "type": "does_not_exist"}],
        })
        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path)
            engine.setup_modules()
        assert len(engine.plugins) == 0

    def test_loads_nested_group(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        cfg_path = _write_config(tmp_path, {
            "database": {"path": db_path},
            "plugins": [{
                "name": "My Group",
                "type": "group",
                "children": [
                    {"name": "Child A", "id": "child-a", "type": "uptime", "target_host": "1.2.3.4"},
                ],
            }],
        })
        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path)
            engine.setup_modules()
        assert len(engine.plugins) == 1
        assert engine.plugins[0].name == "My Group"
        assert len(engine.plugins[0].children) == 1
        assert engine.plugins[0].children[0].name == "Child A"

    def test_bad_plugin_does_not_block_others(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        cfg_path = _write_config(tmp_path, {
            "database": {"path": db_path},
            "plugins": [
                {"name": "Bad",  "type": "does_not_exist"},
                {"name": "Good", "id": "good", "type": "uptime", "target_host": "127.0.0.1"},
            ],
        })
        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path)
            engine.setup_modules()
        assert len(engine.plugins) == 1
        assert engine.plugins[0].name == "Good"


class TestSSHDefaultsMerge:
    """Global ssh_defaults are merged into each plugin's ssh_config."""

    def test_defaults_applied_to_ssh_config(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        cfg_path = _write_config(tmp_path, {
            "database": {"path": db_path},
            "ssh_defaults": {"username": "beatlink", "key_path": "/run/vigil.key"},
            "plugins": [{
                "name": "CPU", "id": "cpu", "type": "cpu_usage",
                "ssh_config": {"host": "server.technet"},
            }],
        })
        with patch("vigil.collector.plugin_base.SSHConnection") as mock_ssh:
            engine = VigilEngine(cfg_path)
            engine.setup_modules()
        passed_cfg = mock_ssh.from_config.call_args[0][0]
        assert passed_cfg["ssh_config"] == {
            "host": "server.technet",
            "username": "beatlink",
            "key_path": "/run/vigil.key",
        }

    def test_plugin_value_overrides_default(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        cfg_path = _write_config(tmp_path, {
            "database": {"path": db_path},
            "ssh_defaults": {"username": "beatlink"},
            "plugins": [{
                "name": "CPU", "id": "cpu", "type": "cpu_usage",
                "ssh_config": {"host": "server.technet", "username": "root"},
            }],
        })
        with patch("vigil.collector.plugin_base.SSHConnection") as mock_ssh:
            engine = VigilEngine(cfg_path)
            engine.setup_modules()
        passed_cfg = mock_ssh.from_config.call_args[0][0]
        assert passed_cfg["ssh_config"]["username"] == "root"

    def test_no_ssh_config_left_untouched(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        cfg_path = _write_config(tmp_path, {
            "database": {"path": db_path},
            "ssh_defaults": {"username": "beatlink"},
            "plugins": [{
                "name": "Host", "id": "host", "type": "uptime",
                "target_host": "127.0.0.1",
            }],
        })
        with patch("vigil.collector.plugin_base.SSHConnection") as mock_ssh:
            engine = VigilEngine(cfg_path)
            engine.setup_modules()
        passed_cfg = mock_ssh.from_config.call_args[0][0]
        assert "ssh_config" not in passed_cfg


class TestLogRetention:
    def test_retention_days_loaded_from_config(self, tmp_path):
        cfg_path = _write_config(tmp_path, {
            "database": {"path": str(tmp_path / "t.db")},
            "logging": {"retention_days": 14},
            "plugins": [],
        })
        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path)
        assert engine.log_retention_days == 14

    def test_maybe_prune_throttles(self, tmp_path):
        cfg_path = _write_config(tmp_path, {
            "database": {"path": str(tmp_path / "t.db")},
            "logging": {"retention_days": 30},
            "plugins": [],
        })
        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path)
        engine.db = MagicMock()
        engine._maybe_prune_logs()          # first call prunes
        engine._maybe_prune_logs()          # immediate second call is throttled
        assert engine.db.prune_logs.call_count == 1
        engine.db.prune_logs.assert_called_with(30)

    def test_prune_disabled_when_retention_zero(self, tmp_path):
        cfg_path = _write_config(tmp_path, {
            "database": {"path": str(tmp_path / "t.db")},
            "logging": {"retention_days": 0},
            "plugins": [],
        })
        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path)
        engine.db = MagicMock()
        engine._maybe_prune_logs()
        engine.db.prune_logs.assert_not_called()


class TestFlatten:
    """_flatten yields every monitor in the tree, groups and leaves alike."""

    def test_flattens_nested_tree(self):
        leaf = MagicMock(id="leaf", children=[])
        mid = MagicMock(id="mid", children=[leaf])
        root = MagicMock(id="root", children=[mid])

        engine = object.__new__(VigilEngine)
        engine.plugins = [root]

        assert list(VigilEngine._flatten(engine.plugins)) == [root, mid, leaf]


class TestPerMonitorScheduling:
    """
    Each monitor is driven by its own independent task/loop (_monitor_loop),
    not a shared tick. A group polls on its own schedule too — it aggregates
    by re-reading live child status from the DB (GroupPlugin.on_collect), so
    it has no ordering dependency on when its children last ran.
    """

    async def test_monitor_loop_reschedules_using_plugin_interval(self):
        # _monitor_loop sleeps `plugin.interval` between calls; patch sleep to
        # observe both the startup jitter and the interval, and to stop the
        # otherwise-infinite loop after a couple of iterations.
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 3:  # jitter, then two interval sleeps
                raise asyncio.CancelledError()

        plugin = MagicMock(id="p", interval=42)
        plugin.run_cycle = AsyncMock(return_value=True)

        engine = object.__new__(VigilEngine)
        with patch("vigil.collector.main.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await engine._monitor_loop(plugin)

        assert plugin.run_cycle.await_count == 2
        # First sleep is the startup jitter (0..STARTUP_JITTER_SECONDS); the
        # rest are the monitor's own interval, independent of any other
        # monitor's schedule.
        assert calls[1:] == [42, 42]

    async def test_a_crashing_monitor_keeps_polling(self):
        # One monitor raising must not end its own loop, let alone anyone else's.
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 3:
                raise asyncio.CancelledError()

        plugin = MagicMock(id="p", interval=5)
        plugin.run_cycle = AsyncMock(side_effect=RuntimeError("boom"))

        engine = object.__new__(VigilEngine)
        with patch("vigil.collector.main.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await engine._monitor_loop(plugin)

        assert plugin.run_cycle.await_count == 2

    async def test_run_starts_one_task_per_flattened_monitor(self, tmp_path):
        cfg_path = str(tmp_path / "config.yaml")
        import yaml
        with open(cfg_path, "w") as fh:
            yaml.dump({"database": {"path": str(tmp_path / "t.db")}, "plugins": []}, fh)

        with patch("vigil.collector.plugin_base.SSHConnection"):
            engine = VigilEngine(cfg_path)

        leaf = MagicMock(id="leaf", children=[], interval=60)
        group = MagicMock(id="group", children=[leaf], interval=60)
        engine.plugins = [group]
        engine._start_exporters = MagicMock()

        created = []
        real_create_task = asyncio.create_task

        def spy_create_task(coro, *a, **kw):
            created.append(coro)
            coro.close()  # never actually run it — just observe scheduling
            return MagicMock()

        with patch("vigil.collector.main.asyncio.create_task", side_effect=spy_create_task):
            with patch.object(engine, "_prune_loop", AsyncMock()):
                await engine.run()

        # One task per monitor (groups included), plus the internal API task.
        monitor_tasks = [c for c in created if c.cr_code.co_name == "_monitor_loop"]
        assert len(monitor_tasks) == 2
        assert any(c.cr_code.co_name == "_run_internal_api" for c in created)


class TestExceptionIsolation:
    """A crashing plugin must not prevent other plugins from running."""

    async def test_exception_does_not_stop_sibling(self):
        ran = []

        async def crash():
            raise RuntimeError("plugin exploded")

        async def ok():
            ran.append("ok")

        mock_bad = MagicMock(id="bad", children=[])
        mock_bad.run_cycle = crash
        mock_ok  = MagicMock(id="ok",  children=[])
        mock_ok.run_cycle = ok

        results = await asyncio.gather(
            mock_bad.run_cycle(),
            mock_ok.run_cycle(),
            return_exceptions=True,
        )

        assert any(isinstance(r, RuntimeError) for r in results)
        assert "ok" in ran
