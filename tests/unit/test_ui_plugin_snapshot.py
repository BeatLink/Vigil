"""
UIPlugin.latest_snapshot() — the read side of the collector/web snapshot
round-trip (see PluginSnapshot's docstring in core/data/database.py and
InternalDatabaseLogger.snapshot()). processes.py and service_list.py depend
on this to render their per-row tables in the split architecture, where
render_ui() runs in a different process from on_collect().
"""
import pytest
from unittest.mock import MagicMock

from vigil.web.plugin_base import UIPlugin


class _ProbeUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        pass


@pytest.fixture
def plugin(db_manager):
    cfg = {"name": "probe", "id": "probe", "target_host": "host1"}
    return _ProbeUIPlugin("probe", cfg, db_manager, collector_client=MagicMock())


class TestLatestSnapshot:
    def test_returns_default_when_never_written(self, plugin):
        assert plugin.latest_snapshot(default=[]) == []
        assert plugin.latest_snapshot() is None

    def test_returns_decoded_json_after_collector_writes(self, plugin, db_manager):
        logger = db_manager.get_logger("host1", "probe", "probe")
        logger.snapshot([{"pid": 1}, {"pid": 2}])
        db_manager.flush()
        assert plugin.latest_snapshot(default=[]) == [{"pid": 1}, {"pid": 2}]

    def test_scoped_by_plugin_id_not_name(self, plugin, db_manager):
        # Same shape of bug this project guards against everywhere else
        # (metrics/events/logs all scoped by id, not display name) — a
        # snapshot written under a different plugin's id must not leak here.
        other_logger = db_manager.get_logger("host1", "probe", "someone-else")
        other_logger.snapshot([{"pid": 99}])
        db_manager.flush()
        assert plugin.latest_snapshot(default=[]) == []

    def test_malformed_json_falls_back_to_default(self, plugin, db_manager):
        db_manager.set_snapshot("probe", "{not valid json")
        db_manager.flush()
        assert plugin.latest_snapshot(default=[]) == []
