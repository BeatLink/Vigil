import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.smart import Smart
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-smart", "id": "test-smart", "ssh_config": {"host": "test.host"}}

def _run(plugin, run_cycle, body, code=0, stderr=""):
    return run_cycle(plugin, lambda c: CmdResult(code, body, stderr))



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-smart"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-smart") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Smart, CFG)


from vigil.plugins.smart import _SMART_SCRIPT


class TestCollection:
    async def test_all_pass_is_online(self, plugin, run_cycle):
        _run(plugin, run_cycle, "PASS /dev/sda\nPASS /dev/sdb")
        assert _latest_status() == "online"

    async def test_one_fail_sets_failed(self, plugin, run_cycle):
        _run(plugin, run_cycle, "PASS /dev/sda\nFAIL /dev/sdb\nFAIL /dev/sdc")
        assert _latest_status() == "failed"
        assert _latest_metric("disks_total") == 3
        assert _latest_metric("disks_ok") == 1
        assert _latest_metric("disks_failed") == 2

    async def test_no_disks_sets_offline(self, plugin, run_cycle):
        _run(plugin, run_cycle, "")
        assert _latest_status() == "offline"

    async def test_malformed_lines_skipped(self, plugin, run_cycle):
        _run(plugin, run_cycle, "PASS /dev/sda\nsome random noise\nFAIL /dev/sdb")
        assert _latest_metric("disks_total") == 2

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        _run(plugin, run_cycle, "", code=-1, stderr="SSH timeout")
        assert _latest_status() == "failed"


class TestBlindChecksAreNotHealthy:
    """The failure that matters most: when smartctl cannot run, the monitor
    must not report healthy disks. Found in production, where a missing sudo
    right made a host report 4 OK disks while reading none of them."""

    def test_a_privilege_error_is_not_a_passing_disk(self, plugin):
        result = plugin.parse([CmdResult(
            0, "UNKNOWN /dev/sda sudo: must be owned by uid 0 and have the setuid bit set", "")])
        assert result.status == 'failed'
        assert result.metrics['disks_ok'] == 0

    def test_a_healthy_disk_still_passes(self, plugin):
        result = plugin.parse([CmdResult(0, "PASS /dev/sda\nPASS /dev/sdb", "")])
        assert result.status == 'online'
        assert result.metrics['disks_ok'] == 2

    def test_one_unreadable_disk_fails_the_monitor(self, plugin):
        result = plugin.parse([CmdResult(0, "PASS /dev/sda\nUNKNOWN /dev/sdb Permission denied", "")])
        assert result.status == 'failed'

    def test_the_script_classifies_by_positive_assertion(self):
        assert "test result: *PASSED" in _SMART_SCRIPT


class TestNonPhysicalDevices:
    """zram, ZFS zvols and loop/md/dm nodes are reported as type "disk" by
    lsblk but have no SMART data of their own."""

    def test_virtual_devices_are_filtered_before_probing(self):
        assert "grep -Ev '^(zram|zd|loop|md|dm-|sr|fd|ram)'" in _SMART_SCRIPT

    def test_an_unsupported_device_is_skipped_not_counted(self, plugin):
        result = plugin.parse([CmdResult(0, "PASS /dev/sda\nSKIP /dev/zd0", "")])
        assert result.status == 'online'
        assert result.metrics['disks_total'] == 1

    def test_skips_alone_look_like_no_disks(self, plugin):
        assert plugin.parse([CmdResult(0, "SKIP /dev/zram0\nSKIP /dev/zd0", "")]).status == 'offline'
