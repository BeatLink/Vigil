import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.smart_disk import SmartDisk
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


SMART_CFG = {
    "name":       "test-smart",
    "id":         "test-smart",
    "ssh_config": {"host": "test.host"},
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(SmartDisk, SMART_CFG)


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



class TestBlindChecksAreNotHealthy:
    """The failure that matters most: when smartctl cannot run, the monitor
    must not report healthy disks. Found in production, where a missing sudo
    right made a host report 4 OK disks while reading none of them."""

    def test_a_privilege_error_is_not_a_passing_disk(self, plugin):
        result = plugin.parse([CmdResult(
            0, "UNKNOWN /dev/sda sudo: must be owned by uid 0 and have the setuid bit set", "")])

        assert result.status == 'failed'
        assert result.metrics['disks_ok'] == 0
        assert result.metrics['disks_unknown'] == 1
        assert any('Could not read SMART health' in m for m, _ in result.logs)

    def test_a_healthy_disk_still_passes(self, plugin):
        result = plugin.parse([CmdResult(0, "PASS /dev/sda\nPASS /dev/sdb", "")])

        assert result.status == 'online'
        assert result.metrics['disks_ok'] == 2
        assert result.metrics['disks_unknown'] == 0

    def test_one_unreadable_disk_fails_the_monitor(self, plugin):
        result = plugin.parse([CmdResult(0, "PASS /dev/sda\nUNKNOWN /dev/sdb Permission denied", "")])

        assert result.status == 'failed'
        assert (result.metrics['disks_ok'], result.metrics['disks_unknown']) == (1, 1)

    def test_a_real_failure_still_fails(self, plugin):
        result = plugin.parse([CmdResult(0, "FAIL /dev/sda", "")])
        assert result.status == 'failed'
        assert result.metrics['disks_failed'] == 1

    def test_the_script_classifies_by_positive_assertion(self):
        """Guards the shell itself: a blind check must land in the UNKNOWN
        branch rather than falling through to PASS."""
        from vigil.plugins.smart_disk import _SMART_SCRIPT
        assert "test result: *PASSED" in _SMART_SCRIPT
        assert "UNKNOWN $d" in _SMART_SCRIPT
