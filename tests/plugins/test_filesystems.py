import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.filesystems import FilesystemsPlugin, _sanitize
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {"name": "test-fs", "id": "test-fs", "warning": 80, "threshold": 90,
            "ssh_config": {"host": "test.host"}}


def _latest_status(pid="test-fs"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric, name="test-fs"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


# df --output=target,size,used,pcent, with a header line first.
_HEADER = "Mounted on           1B-blocks        Used Use%"


def _df(*rows):
    lines = [_HEADER]
    for mount, size, used, pct in rows:
        lines.append(f"{mount} {size} {used} {pct}%")
    return "\n".join(lines) + "\n"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(FilesystemsPlugin, BASE_CFG)


class TestSanitize:
    def test_root(self):
        assert _sanitize('/') == 'root'

    def test_nested(self):
        assert _sanitize('/var/log') == 'var_log'


class TestFilesystemsCollection:
    async def test_all_healthy_online(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _df(
            ("/", 100_000_000_000, 40_000_000_000, 40),
            ("/home", 500_000_000_000, 100_000_000_000, 20),
        ), ""))
        await plugin.on_collect()
        assert _latest_status() == "online"
        assert _latest_metric("worst_used_pct") == pytest.approx(40.0)
        assert _latest_metric("fs_root_used_pct") == pytest.approx(40.0)
        assert _latest_metric("fs_home_used_pct") == pytest.approx(20.0)

    async def test_over_warning(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _df(
            ("/", 100, 85, 85),
        ), ""))
        await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_over_threshold_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _df(
            ("/", 100, 40, 40),
            ("/data", 100, 95, 95),
        ), ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"
        assert _latest_metric("worst_used_pct") == pytest.approx(95.0)

    async def test_mount_with_space(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _df(
            ("/mnt/my drive", 100, 10, 10),
        ), ""))
        await plugin.on_collect()
        assert _latest_status() == "online"
        assert _latest_metric("fs_mnt_my_drive_used_pct") == pytest.approx(10.0)

    async def test_no_filesystems_offline(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _HEADER + "\n", ""))
        await plugin.on_collect()
        assert _latest_status() == "offline"

    async def test_df_failure_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(1, "", "df: error"))
        await plugin.on_collect()
        assert _latest_status() == "failed"


class TestFilesystemsActions:
    async def test_on_action_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
