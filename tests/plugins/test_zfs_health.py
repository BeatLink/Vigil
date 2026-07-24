import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.zfs_health import ZFSHealth
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


HEALTH_CFG = {
    "name":       "test-zfs-health",
    "id":         "test-zfs-health",
    "ssh_config": {"host": "test.host"},
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(ZFSHealth, HEALTH_CFG)


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
    async def test_all_online_is_ok(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "pool1\tONLINE\npool2\tONLINE", ""))
        assert _latest_status() == "online"

    async def test_all_online_metrics(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "pool1\tONLINE\npool2\tONLINE", ""))
        assert _latest_metric("pools_total") == 2
        assert _latest_metric("pools_ok") == 2
        assert _latest_metric("pools_degraded") == 0

    async def test_degraded_pool_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "pool1\tONLINE\npool2\tDEGRADED", ""))
        assert _latest_status() == "failed"

    async def test_degraded_metrics(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "pool1\tDEGRADED\npool2\tONLINE", ""))
        assert _latest_metric("pools_ok") == 1
        assert _latest_metric("pools_degraded") == 1

    @pytest.mark.parametrize("bad_state", ["DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"])
    async def test_all_unhealthy_states_trigger_failed(self, plugin, run_cycle, bad_state):
        run_cycle(plugin, lambda c: CmdResult(0, f"pool1\t{bad_state}", ""))
        assert _latest_status() == "failed", f"Expected failed for state {bad_state}"

    async def test_no_pools_sets_offline(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "", ""))
        assert _latest_status() == "offline"

    async def test_malformed_lines_skipped(self, plugin, run_cycle):
        output = "pool1\tONLINE\njust_one_word\npool2\tONLINE"
        run_cycle(plugin, lambda c: CmdResult(0, output, ""))
        assert _latest_metric("pools_total") == 2

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(-1, "", "timeout"))
        assert _latest_status() == "failed"

    async def test_on_action_always_false(self, plugin):
        assert plugin.plan_action("scrub") is None
