import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.zfs_health import ZFSHealthCollectorPlugin
from vigil.core.data.database import db, StatusHistory, Metric


HEALTH_CFG = {
    "name":       "test-zfs-health",
    "id":         "test-zfs-health",
    "ssh_config": {"host": "test.host"},
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(ZFSHealthCollectorPlugin, HEALTH_CFG)


def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-zfs-health"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(name: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-zfs-health") & (Metric.metric_name == name)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestZFSHealthCollection:
    async def test_all_online_is_ok(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, "pool1\tONLINE\npool2\tONLINE", "")
        )
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_all_online_metrics(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, "pool1\tONLINE\npool2\tONLINE", "")
        )
        await plugin.on_collect()
        assert _latest_metric("pools_total") == 2
        assert _latest_metric("pools_ok") == 2
        assert _latest_metric("pools_degraded") == 0

    async def test_degraded_pool_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, "pool1\tONLINE\npool2\tDEGRADED", "")
        )
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_degraded_metrics(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, "pool1\tDEGRADED\npool2\tONLINE", "")
        )
        await plugin.on_collect()
        assert _latest_metric("pools_ok") == 1
        assert _latest_metric("pools_degraded") == 1

    @pytest.mark.parametrize("bad_state", ["DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"])
    async def test_all_unhealthy_states_trigger_failed(self, plugin, bad_state):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, f"pool1\t{bad_state}", "")
        )
        await plugin.on_collect()
        assert _latest_status() == "failed", f"Expected failed for state {bad_state}"

    async def test_no_pools_sets_offline(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, "", ""))
        await plugin.on_collect()
        assert _latest_status() == "offline"

    async def test_malformed_lines_skipped(self, plugin):
        output = "pool1\tONLINE\njust_one_word\npool2\tONLINE"
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, output, ""))
        await plugin.on_collect()
        assert _latest_metric("pools_total") == 2

    async def test_ssh_failure_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(-1, "", "timeout")
        )
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_on_action_always_false(self, plugin):
        assert await plugin.on_action("scrub") is False
