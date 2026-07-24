import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.zfs_pool import ZFSPool
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


POOL_CFG = {
    "name":       "test-pool",
    "id":         "test-pool",
    "pool":       "data-pool",
    "threshold":  90,
    "ssh_config": {"host": "test.host"},
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(ZFSPool, POOL_CFG)


def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-pool"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_usage() -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-pool") & (Metric.metric_name == "usage_pct")
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestZFSPoolCollection:
    async def test_below_threshold_is_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "data-pool\t50%", ""))
        assert _latest_status() == "online"

    async def test_usage_metric_recorded(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "data-pool\t50%", ""))
        assert _latest_usage() == pytest.approx(50.0)

    async def test_at_threshold_is_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "data-pool\t90%", ""))
        assert _latest_status() == "failed"

    async def test_above_threshold_is_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "data-pool\t95%", ""))
        assert _latest_status() == "failed"

    async def test_high_usage_metric_recorded(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "data-pool\t95%", ""))
        assert _latest_usage() == pytest.approx(95.0)

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(-1, "", "connection timed out"))
        assert _latest_status() == "failed"

    async def test_malformed_output_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "unexpected output", ""))
        assert _latest_status() == "failed"

    async def test_custom_threshold_respected(self, make_plugin, run_cycle):
        plugin = make_plugin(ZFSPool, {**POOL_CFG, "id": "pool-75", "threshold": 75})
        run_cycle(plugin, lambda c: CmdResult(0, "data-pool\t80%", ""))
        with db.connection_context():
            row = StatusHistory.select().where(
                StatusHistory.collector_id == "pool-75"
            ).order_by(StatusHistory.timestamp.desc()).first()
        assert row.state == "failed"

    async def test_on_action_always_false(self, plugin):
        assert plugin.plan_action("anything") is None
