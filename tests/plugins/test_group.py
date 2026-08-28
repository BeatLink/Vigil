import pytest
from unittest.mock import MagicMock
from vigil.plugins.group import Group
from vigil.core.connectors.types import Status
from vigil.core.database.database import db, StatusHistory


GROUP_CFG = {
    "name":     "test-group",
    "id":       "test-group",
    "type":     "group",
    "interval": 60,
}


@pytest.fixture
def group(make_plugin):
    return make_plugin(Group, GROUP_CFG)


def _make_child(plugin_id: str, status: str, db_manager) -> MagicMock:
    db_manager.insert_status(plugin_id, status)
    child = MagicMock()
    child.id = plugin_id
    child.name = f"Child {plugin_id}"
    child.children = []
    return child


def _aggregated(group, db_manager):
    return group._aggregate_status(db_manager.latest_statuses())


class TestSeverityOrder:
    def test_online_is_least_severe(self):
        assert Status.ONLINE.severity == 0

    def test_failed_is_most_severe(self):
        assert Status.FAILED.severity > Status.WARNING.severity
        assert Status.WARNING.severity > Status.OFFLINE.severity
        assert Status.OFFLINE.severity > Status.ONLINE.severity


class TestStatusAggregation:
    def test_all_online_aggregates_online(self, group, db_manager):
        group.children = [
            _make_child("a", "online", db_manager),
            _make_child("b", "online", db_manager),
        ]
        assert _aggregated(group, db_manager) == "online"

    def test_one_failed_aggregates_failed(self, group, db_manager):
        group.children = [
            _make_child("a", "online", db_manager),
            _make_child("b", "failed", db_manager),
        ]
        assert _aggregated(group, db_manager) == "failed"

    def test_failed_beats_warning(self, group, db_manager):
        group.children = [
            _make_child("a", "warning", db_manager),
            _make_child("b", "failed", db_manager),
        ]
        assert _aggregated(group, db_manager) == "failed"

    def test_warning_beats_online(self, group, db_manager):
        group.children = [
            _make_child("a", "online", db_manager),
            _make_child("b", "warning", db_manager),
        ]
        assert _aggregated(group, db_manager) == "warning"

    def test_warning_beats_offline(self, group, db_manager):
        group.children = [
            _make_child("a", "offline", db_manager),
            _make_child("b", "warning", db_manager),
        ]
        assert _aggregated(group, db_manager) == "warning"

    def test_no_children_returns_online(self, group, db_manager):
        group.children = []
        assert _aggregated(group, db_manager) == "online"

    def test_child_with_no_history_treated_as_offline(self, group, db_manager):
        child = MagicMock()
        child.id = "never-polled"
        child.children = []
        group.children = [child]
        assert _aggregated(group, db_manager) == "offline"

    def test_mixed_online_and_offline_returns_offline(self, group, db_manager):
        group.children = [
            _make_child("a", "online", db_manager),
            _make_child("b", "offline", db_manager),
        ]
        assert _aggregated(group, db_manager) == "offline"

    def test_all_failed_returns_failed(self, group, db_manager):
        group.children = [
            _make_child("a", "failed", db_manager),
            _make_child("b", "failed", db_manager),
        ]
        assert _aggregated(group, db_manager) == "failed"


class TestOnCollect:
    async def test_writes_aggregated_status_to_db(self, group, db_manager, run_requests):
        group.children = [_make_child("child-x", "online", db_manager)]
        run_requests(group)
        with db.connection_context():
            row = StatusHistory.select().where(
                StatusHistory.collector_id == "test-group"
            ).order_by(StatusHistory.timestamp.desc()).first()
        assert row is not None
        assert row.state == "online"

    async def test_propagates_failed_child_to_group(self, group, db_manager, run_requests):
        group.children = [
            _make_child("child-ok", "online", db_manager),
            _make_child("child-bad", "failed", db_manager),
        ]
        run_requests(group)
        with db.connection_context():
            row = StatusHistory.select().where(
                StatusHistory.collector_id == "test-group"
            ).order_by(StatusHistory.timestamp.desc()).first()
        assert row.state == "failed"

    async def test_on_action_always_false(self, group):
        assert group.plan_action("restart") is None
