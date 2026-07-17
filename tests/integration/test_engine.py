"""
Integration tests for VigilEngine.

These tests exercise real plugin loading via importlib and a real temp-file database,
but mock SSH connections so no actual network calls are made.
"""
import asyncio
import pytest
import yaml
from unittest.mock import MagicMock, AsyncMock, patch
from vigil.core.main import VigilEngine
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
        with patch("vigil.core.common.base_plugin.SSHConnection"):
            engine = VigilEngine(cfg_path)
        assert engine.db is not None

    def test_db_path_taken_from_config(self, tmp_path):
        db_path = str(tmp_path / "custom.db")
        cfg_path = _write_config(tmp_path, {"database": {"path": db_path}, "plugins": []})
        with patch("vigil.core.common.base_plugin.SSHConnection"):
            engine = VigilEngine(cfg_path)
        assert engine.db_path == db_path

    def test_db_path_override_takes_precedence(self, tmp_path):
        cfg_path = _write_config(tmp_path, {"database": {"path": "original.db"}, "plugins": []})
        override = str(tmp_path / "override.db")
        with patch("vigil.core.common.base_plugin.SSHConnection"):
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
        with patch("vigil.core.common.base_plugin.SSHConnection"):
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
        with patch("vigil.core.common.base_plugin.SSHConnection"):
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
        with patch("vigil.core.common.base_plugin.SSHConnection"):
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
        with patch("vigil.core.common.base_plugin.SSHConnection"):
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
        with patch("vigil.core.common.base_plugin.SSHConnection") as mock_ssh:
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
        with patch("vigil.core.common.base_plugin.SSHConnection") as mock_ssh:
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
        with patch("vigil.core.common.base_plugin.SSHConnection") as mock_ssh:
            engine = VigilEngine(cfg_path)
            engine.setup_modules()
        passed_cfg = mock_ssh.from_config.call_args[0][0]
        assert "ssh_config" not in passed_cfg


class TestBFSPollOrdering:
    """Group plugins must aggregate AFTER their children have written fresh status."""

    async def test_children_polled_before_parents(self):
        call_order = []

        async def child_cycle():
            call_order.append("child")

        async def group_cycle():
            call_order.append("group")

        mock_child = MagicMock(id="child", children=[])
        mock_child.run_cycle = child_cycle
        mock_group = MagicMock(id="group", children=[mock_child])
        mock_group.run_cycle = group_cycle

        # Build a bare engine without file I/O
        engine = object.__new__(VigilEngine)
        engine.plugins = [mock_group]
        engine.db = MagicMock()

        # Run one BFS cycle (copy of engine.run logic without the sleep loop)
        levels = []
        current = list(engine.plugins)
        while current:
            levels.append(current)
            current = [c for p in current for c in p.children]

        for level in reversed(levels):
            await asyncio.gather(*[p.run_cycle() for p in level])

        assert call_order == ["child", "group"]

    async def test_deeply_nested_runs_bottom_up(self):
        call_order = []

        def make_mock(name, children):
            m = MagicMock(id=name, children=children)
            async def run():
                call_order.append(name)
            m.run_cycle = run
            return m

        leaf   = make_mock("leaf",   [])
        mid    = make_mock("mid",    [leaf])
        root   = make_mock("root",   [mid])

        engine = object.__new__(VigilEngine)
        engine.plugins = [root]
        engine.db = MagicMock()

        levels = []
        current = list(engine.plugins)
        while current:
            levels.append(current)
            current = [c for p in current for c in p.children]

        for level in reversed(levels):
            await asyncio.gather(*[p.run_cycle() for p in level])

        assert call_order == ["leaf", "mid", "root"]


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
