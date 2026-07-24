import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.smart_disk import SmartDiskCollectorPlugin
from vigil.collector.orchestration.types import CmdResult
from vigil.core.data.database import db, StatusHistory, Metric


SMART_CFG = {
    "name":       "test-smart",
    "id":         "test-smart",
    "ssh_config": {"host": "test.host"},
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(SmartDiskCollectorPlugin, SMART_CFG)


def _latest_status(plugin_id: str) -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(name: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-smart") & (Metric.metric_name == name)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestSmartDiskCollection:
    async def test_all_pass_is_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "PASS /dev/sda\nPASS /dev/sdb", ""))
        assert _latest_status("test-smart") == "online"

    async def test_all_pass_metrics(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "PASS /dev/sda\nPASS /dev/sdb", ""))
        assert _latest_metric("disks_total") == 2
        assert _latest_metric("disks_ok") == 2
        assert _latest_metric("disks_failed") == 0

    async def test_one_fail_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "PASS /dev/sda\nFAIL /dev/sdb", ""))
        assert _latest_status("test-smart") == "failed"

    async def test_fail_metrics_correct(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "PASS /dev/sda\nFAIL /dev/sdb\nFAIL /dev/sdc", ""))
        assert _latest_metric("disks_total") == 3
        assert _latest_metric("disks_ok") == 1
        assert _latest_metric("disks_failed") == 2

    async def test_all_fail_is_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "FAIL /dev/sda", ""))
        assert _latest_status("test-smart") == "failed"

    async def test_no_output_sets_offline(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "", ""))
        assert _latest_status("test-smart") == "offline"

    async def test_malformed_lines_skipped(self, plugin, run_cycle):
        output = "PASS /dev/sda\nsome random noise\nFAIL /dev/sdb"
        run_cycle(plugin, lambda c: CmdResult(0, output, ""))
        assert _latest_metric("disks_total") == 2

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(-1, "", "SSH timeout"))
        assert _latest_status("test-smart") == "failed"

    async def test_on_action_always_false(self, plugin):
        assert plugin.plan_action("anything") is None
