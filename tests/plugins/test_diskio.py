import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.diskio import DiskIo, _parse_diskstats, _is_physical, _auto_detect_device, _format_rate
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-diskio",
    "id":   "test-diskio",
    "ssh_config": {"host": "test.host"},
}


def _make_diskstats(devices: dict) -> str:
    lines = []
    for i, (name, (rd, wr)) in enumerate(devices.items()):
        lines.append(f"   8       {i} {name} 100 0 {rd} 50 200 0 {wr} 80 0 0 0 0")
    return "\n".join(lines) + "\n"


def _two_snaps(d1: dict, d2: dict) -> str:
    return _make_diskstats(d1) + "---SNAP---\n" + _make_diskstats(d2)


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(DiskIo, BASE_CFG)


@pytest.fixture
def explicit_plugin(make_plugin):
    return make_plugin(DiskIo, {**BASE_CFG, "name": "test-diskio-x", "id": "test-diskio-x", "device": "sda"})


def _latest_status(plugin_id: str = "test-diskio"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(name: str, metric: str):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestParseDiskstats:
    def test_parses_read_write_sectors(self):
        block = _make_diskstats({"sda": (1000, 2000)})
        assert _parse_diskstats(block)["sda"] == (1000, 2000)

    def test_multiple_devices(self):
        block = _make_diskstats({"sda": (1, 2), "nvme0n1": (3, 4)})
        result = _parse_diskstats(block)
        assert set(result) == {"sda", "nvme0n1"}


class TestIsPhysical:
    def test_whole_disks_are_physical(self):
        assert _is_physical("sda")
        assert _is_physical("nvme0n1")
        assert _is_physical("mmcblk0")

    def test_partitions_not_physical(self):
        assert not _is_physical("sda1")
        assert not _is_physical("nvme0n1p1")
        assert not _is_physical("mmcblk0p2")

    def test_virtual_not_physical(self):
        assert not _is_physical("loop0")
        assert not _is_physical("ram0")
        assert not _is_physical("dm-0")


class TestAutoDetect:
    def test_picks_busiest_disk(self):
        s1 = {"sda": (0, 0), "sdb": (0, 0)}
        s2 = {"sda": (100, 0), "sdb": (5000, 0)}
        assert _auto_detect_device(s1, s2) == "sdb"

    def test_excludes_partitions(self):
        s1 = {"sda1": (0, 0), "sda": (0, 0)}
        s2 = {"sda1": (9999, 0), "sda": (10, 0)}
        assert _auto_detect_device(s1, s2) == "sda"


class TestFormatRate:
    def test_kbps(self):
        assert _format_rate(512.0) == "512.0 KB/s"

    def test_mbps(self):
        assert _format_rate(2048.0) == "2.0 MB/s"


class TestDiskIoCollection:
    async def test_normal_online(self, plugin, run_cycle):
        stdout = _two_snaps({"sda": (0, 0)}, {"sda": (2, 4)})
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "online"
        assert _latest_metric("test-diskio", "read_kbps") == pytest.approx(1.0)
        assert _latest_metric("test-diskio", "write_kbps") == pytest.approx(2.0)

    async def test_counter_reset_clamped(self, plugin, run_cycle):
        stdout = _two_snaps({"sda": (5000, 5000)}, {"sda": (10, 10)})
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_metric("test-diskio", "read_kbps") == pytest.approx(0.0)

    async def test_auto_detects_device(self, plugin, run_cycle):
        stdout = _two_snaps({"sda": (0, 0), "sdb": (0, 0)}, {"sda": (2, 0), "sdb": (1000, 0)})
        result = run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert result.settings[f"diskio:{plugin.id}:active_device"] == "sdb"

    async def test_explicit_device_missing_fails(self, explicit_plugin, run_cycle):
        stdout = _two_snaps({"sdb": (0, 0)}, {"sdb": (2, 0)})
        run_cycle(explicit_plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status("test-diskio-x") == "failed"

    async def test_malformed_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "no separator here", ""))
        assert _latest_status() == "failed"

    async def test_ssh_failure_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(-1, "", "err"))
        assert _latest_status() == "failed"


class TestDiskIoActions:
    async def test_on_action_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
