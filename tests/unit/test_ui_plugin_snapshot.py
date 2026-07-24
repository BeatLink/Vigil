import pytest
from typing import List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult


class _Probe(Plugin):
    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def render_ui(self, context: str = 'page'):
        pass


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(_Probe, {"name": "probe", "id": "probe",
                                         "ssh_config": {"host": "host1"}})


class TestLatestSnapshot:
    def test_returns_default_when_never_written(self, plugin):
        assert plugin.storage.latest_snapshot(default=[]) == []
        assert plugin.storage.latest_snapshot() is None

    def test_returns_decoded_json_after_collector_writes(self, plugin, db_manager):
        logger = db_manager.get_logger("host1", "probe", "probe")
        logger.snapshot([{"pid": 1}, {"pid": 2}])
        db_manager.flush()
        assert plugin.storage.latest_snapshot(default=[]) == [{"pid": 1}, {"pid": 2}]

    def test_scoped_by_plugin_id_not_name(self, plugin, db_manager):
        other_logger = db_manager.get_logger("host1", "probe", "someone-else")
        other_logger.snapshot([{"pid": 99}])
        db_manager.flush()
        assert plugin.storage.latest_snapshot(default=[]) == []

    def test_malformed_json_falls_back_to_default(self, plugin, db_manager):
        db_manager.set_snapshot("probe", "{not valid json")
        db_manager.flush()
        assert plugin.storage.latest_snapshot(default=[]) == []
