import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.disk_space import DiskSpaceCollectorPlugin, _format_gb
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name":      "test-disk",
    "id":        "test-disk",
    "path":      "/",
    "threshold": 90,
    "ssh_config": {"host": "test.host"},
}

def _df_line(size: int, used: int, avail: int, pct: int) -> str:
    return f"{size} {used} {avail} {pct}%\n"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(DiskSpaceCollectorPlugin, BASE_CFG)


@pytest.fixture
def storage_plugin(make_plugin):
    return make_plugin(DiskSpaceCollectorPlugin, {
        "name": "test-disk-storage",
        "id":   "test-disk-storage",
        "path": "/Storage",
        "threshold": 80,
        "ssh_config": {"host": "test.host"},
    })


def _latest_status(plugin_id: str = "test-disk") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(name: str, metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestFormatGb:
    def test_less_than_1gb_shows_mb(self):
        assert _format_gb(0.5) == "512 MB"

    def test_exactly_1gb(self):
        assert _format_gb(1.0) == "1.0 GB"

    def test_between_1_and_1024_shows_gb(self):
        assert _format_gb(512.0) == "512.0 GB"

    def test_1024gb_shows_tb(self):
        assert _format_gb(1024.0) == "1.0 TB"

    def test_above_1024gb_shows_tb(self):
        assert _format_gb(2048.0) == "2.0 TB"


class TestDiskSpaceCollection:
    async def test_below_threshold_sets_online(self, plugin):
        GB = 1024 ** 3
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _df_line(100 * GB, 50 * GB, 50 * GB, 50), "")
        )
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_at_threshold_sets_failed(self, plugin):
        GB = 1024 ** 3
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _df_line(100 * GB, 90 * GB, 10 * GB, 90), "")
        )
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_above_threshold_sets_failed(self, plugin):
        GB = 1024 ** 3
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _df_line(100 * GB, 95 * GB, 5 * GB, 95), "")
        )
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_custom_threshold_respected(self, storage_plugin):
        GB = 1024 ** 3
        storage_plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _df_line(100 * GB, 85 * GB, 15 * GB, 85), "")
        )
        await storage_plugin.on_collect()
        assert _latest_status("test-disk-storage") == "failed"

    async def test_used_pct_metric_recorded(self, plugin):
        GB = 1024 ** 3
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _df_line(100 * GB, 42 * GB, 58 * GB, 42), "")
        )
        await plugin.on_collect()
        assert _latest_metric("test-disk", "used_pct") == pytest.approx(42.0)

    async def test_size_gb_metric_recorded(self, plugin):
        GB = 1024 ** 3
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _df_line(200 * GB, 100 * GB, 100 * GB, 50), "")
        )
        await plugin.on_collect()
        assert _latest_metric("test-disk", "size_gb") == pytest.approx(200.0)

    async def test_used_gb_metric_recorded(self, plugin):
        GB = 1024 ** 3
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _df_line(200 * GB, 75 * GB, 125 * GB, 37), "")
        )
        await plugin.on_collect()
        assert _latest_metric("test-disk", "used_gb") == pytest.approx(75.0)

    async def test_avail_gb_metric_recorded(self, plugin):
        GB = 1024 ** 3
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _df_line(200 * GB, 75 * GB, 125 * GB, 37), "")
        )
        await plugin.on_collect()
        assert _latest_metric("test-disk", "avail_gb") == pytest.approx(125.0)

    async def test_ssh_failure_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(-1, "", "connection refused")
        )
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_malformed_output_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, "unexpected garbage", "")
        )
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_empty_output_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, "", "")
        )
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_no_metrics_written_on_ssh_failure(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(-1, "", "timeout")
        )
        await plugin.on_collect()
        assert _latest_metric("test-disk", "used_pct") is None

    async def test_fractional_percent_parsed(self, plugin):
        GB = 1024 ** 3
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _df_line(100 * GB, 33 * GB, 67 * GB, 33), "")
        )
        await plugin.on_collect()
        assert _latest_metric("test-disk", "used_pct") == pytest.approx(33.0)


class TestDiskSpaceActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
