import pytest
from unittest.mock import MagicMock, patch
from vigil.plugins.group import GroupCollectorPlugin, SEVERITY_ORDER
from vigil.core.data.database import db, StatusHistory


GROUP_CFG = {
    "name":     "test-group",
    "id":       "test-group",
    "type":     "group",
    "interval": 60,
}


@pytest.fixture
def group(db_manager):
    with patch("vigil.collector.plugin_base.SSHConnection") as MockSSH, \
         patch("vigil.collector.plugin_base.SSHCollector"), \
         patch("vigil.collector.plugin_base.SSHController"):
        MockSSH.from_config.return_value = MagicMock(host="localhost")
        plugin = GroupCollectorPlugin("test-group", GROUP_CFG, db_manager)
    return plugin


def _make_child(plugin_id: str, status: str, db_manager) -> MagicMock:
    db_manager.insert_status(plugin_id, status)
    child = MagicMock()
    child.id = plugin_id
    child.name = f"Child {plugin_id}"
    child.children = []
    return child


class TestSeverityOrder:
    def test_online_is_least_severe(self):
        assert SEVERITY_ORDER["online"] == 0

    def test_failed_is_most_severe(self):
        assert SEVERITY_ORDER["failed"] > SEVERITY_ORDER["warning"]
        assert SEVERITY_ORDER["warning"] > SEVERITY_ORDER["offline"]
        assert SEVERITY_ORDER["offline"] > SEVERITY_ORDER["online"]


class TestStatusAggregation:
    def test_all_online_aggregates_online(self, group, db_manager):
        group.children = [
            _make_child("a", "online", db_manager),
            _make_child("b", "online", db_manager),
        ]
        assert group._get_aggregated_status() == "online"

    def test_one_failed_aggregates_failed(self, group, db_manager):
        group.children = [
            _make_child("a", "online", db_manager),
            _make_child("b", "failed", db_manager),
        ]
        assert group._get_aggregated_status() == "failed"

    def test_failed_beats_warning(self, group, db_manager):
        group.children = [
            _make_child("a", "warning", db_manager),
            _make_child("b", "failed", db_manager),
        ]
        assert group._get_aggregated_status() == "failed"

    def test_warning_beats_online(self, group, db_manager):
        group.children = [
            _make_child("a", "online", db_manager),
            _make_child("b", "warning", db_manager),
        ]
        assert group._get_aggregated_status() == "warning"

    def test_warning_beats_offline(self, group, db_manager):
        group.children = [
            _make_child("a", "offline", db_manager),
            _make_child("b", "warning", db_manager),
        ]
        assert group._get_aggregated_status() == "warning"

    def test_no_children_returns_online(self, group):
        group.children = []
        assert group._get_aggregated_status() == "online"

    def test_child_with_no_history_treated_as_offline(self, group):
        child = MagicMock()
        child.id = "never-polled"
        child.children = []
        group.children = [child]
        assert group._get_aggregated_status() == "offline"

    def test_mixed_online_and_offline_returns_offline(self, group, db_manager):
        group.children = [
            _make_child("a", "online", db_manager),
            _make_child("b", "offline", db_manager),
        ]
        assert group._get_aggregated_status() == "offline"

    def test_all_failed_returns_failed(self, group, db_manager):
        group.children = [
            _make_child("a", "failed", db_manager),
            _make_child("b", "failed", db_manager),
        ]
        assert group._get_aggregated_status() == "failed"


class TestOnCollect:
    async def test_writes_aggregated_status_to_db(self, group, db_manager):
        group.children = [_make_child("child-x", "online", db_manager)]
        await group.on_collect()
        with db.connection_context():
            row = StatusHistory.select().where(
                StatusHistory.collector_id == "test-group"
            ).order_by(StatusHistory.timestamp.desc()).first()
        assert row is not None
        assert row.state == "online"

    async def test_propagates_failed_child_to_group(self, group, db_manager):
        group.children = [
            _make_child("child-ok", "online", db_manager),
            _make_child("child-bad", "failed", db_manager),
        ]
        await group.on_collect()
        with db.connection_context():
            row = StatusHistory.select().where(
                StatusHistory.collector_id == "test-group"
            ).order_by(StatusHistory.timestamp.desc()).first()
        assert row.state == "failed"

    async def test_on_action_always_false(self, group):
        assert await group.on_action("restart") is False
