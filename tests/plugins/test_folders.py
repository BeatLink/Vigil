import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.folders import FoldersCollectorPlugin, _sanitize
from vigil.core.data.database import db, StatusHistory, Metric

_GB = 1024 ** 3


def _latest_status(pid="test-folders"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric, name="test-folders"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _cfg(**extra):
    base = {"name": "test-folders", "id": "test-folders", "ssh_config": {"host": "test.host"}}
    base.update(extra)
    return base


class TestFoldersCollection:
    async def test_under_thresholds_online(self, make_plugin):
        p = make_plugin(FoldersCollectorPlugin, _cfg(folders=[
            {"path": "/var/log", "warning": 5, "threshold": 10},
        ]))
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, f"{2 * _GB}\t/var/log", ""))
        await p.on_collect()
        assert _latest_status() == "online"
        assert _latest_metric("folder_var_log_gb") == pytest.approx(2.0)

    async def test_over_warning(self, make_plugin):
        p = make_plugin(FoldersCollectorPlugin, _cfg(folders=[
            {"path": "/data", "warning": 5, "threshold": 10},
        ]))
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, f"{7 * _GB}\t/data", ""))
        await p.on_collect()
        assert _latest_status() == "warning"

    async def test_over_threshold_failed(self, make_plugin):
        p = make_plugin(FoldersCollectorPlugin, _cfg(folders=[
            {"path": "/data", "warning": 5, "threshold": 10},
        ]))
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, f"{12 * _GB}\t/data", ""))
        await p.on_collect()
        assert _latest_status() == "failed"

    async def test_worst_across_folders(self, make_plugin):
        p = make_plugin(FoldersCollectorPlugin, _cfg(folders=[
            {"path": "/a", "warning": 5, "threshold": 10},
            {"path": "/b", "warning": 5, "threshold": 10},
        ]))
        p.ssh_collector.fetch_output = AsyncMock(side_effect=[
            (0, f"{2 * _GB}\t/a", ""),
            (0, f"{12 * _GB}\t/b", ""),
        ])
        await p.on_collect()
        assert _latest_status() == "failed"

    async def test_size_only_no_thresholds_online(self, make_plugin):
        p = make_plugin(FoldersCollectorPlugin, _cfg(folders=[{"path": "/media"}]))
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, f"{999 * _GB}\t/media", ""))
        await p.on_collect()
        assert _latest_status() == "online"

    async def test_du_timeout_failed(self, make_plugin):
        p = make_plugin(FoldersCollectorPlugin, _cfg(folders=[{"path": "/huge"}], timeout=1))
        p.ssh_collector.fetch_output = AsyncMock(return_value=(124, "", ""))
        await p.on_collect()
        assert _latest_status() == "failed"

    async def test_missing_folder_failed(self, make_plugin):
        p = make_plugin(FoldersCollectorPlugin, _cfg(folders=[{"path": "/nope"}]))
        p.ssh_collector.fetch_output = AsyncMock(return_value=(1, "", "du: cannot access"))
        await p.on_collect()
        assert _latest_status() == "failed"

    async def test_no_folders_offline(self, make_plugin):
        p = make_plugin(FoldersCollectorPlugin, _cfg())
        await p.on_collect()
        assert _latest_status() == "offline"


class TestFoldersActions:
    async def test_on_action_returns_false(self, make_plugin):
        p = make_plugin(FoldersCollectorPlugin, _cfg(folders=[{"path": "/x"}]))
        assert await p.on_action("anything") is False
