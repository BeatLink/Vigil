import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.disk_io import DiskIo
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-disk_io", "id": "test-disk_io", "ssh_config": {"host": "test.host"}}

def _make_diskstats(devices: dict) -> str:
    lines = []
    for i, (name, (rd, wr)) in enumerate(devices.items()):
        lines.append(f"   8       {i} {name} 100 0 {rd} 50 200 0 {wr} 80 0 0 0 0")
    return "\n".join(lines) + "\n"


def _two_snaps(d1: dict, d2: dict) -> str:
    return _make_diskstats(d1) + "---SNAP---\n" + _make_diskstats(d2)


def _run(plugin, run_cycle, body, code=0, stderr=""):
    return run_cycle(plugin, lambda c: CmdResult(code, body, stderr))



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-disk_io"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-disk_io") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(DiskIo, CFG)


from vigil.plugins.disk_io import (
    _parse_diskstats, _is_physical, _auto_detect_device, _format_rate,
)


class TestParseDiskstats:
    def test_parses_read_write_sectors(self):
        assert _parse_diskstats(_make_diskstats({"sda": (1000, 2000)}))["sda"] == (1000, 2000)

    def test_multiple_devices(self):
        result = _parse_diskstats(_make_diskstats({"sda": (1, 2), "nvme0n1": (3, 4)}))
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
        assert _auto_detect_device({"sda": (0, 0), "sdb": (0, 0)},
                                   {"sda": (100, 0), "sdb": (5000, 0)}) == "sdb"

    def test_excludes_partitions(self):
        assert _auto_detect_device({"sda1": (0, 0), "sda": (0, 0)},
                                   {"sda1": (9999, 0), "sda": (10, 0)}) == "sda"

    def test_returns_none_when_no_candidates(self):
        assert _auto_detect_device({}, {}) is None


class TestFormatRate:
    def test_below_1024_shows_kbps(self):
        assert _format_rate(512.0) == "512.0 KB/s"

    def test_at_1024_shows_mbps(self):
        assert _format_rate(2048.0) == "2.0 MB/s"


class TestCollection:
    async def test_throughput_recorded(self, plugin, run_cycle):
        _run(plugin, run_cycle, _two_snaps({"sda": (0, 0)}, {"sda": (2, 4)}))
        assert _latest_status() == "online"
        assert _latest_metric("read_kbps") == pytest.approx(1.0)
        assert _latest_metric("write_kbps") == pytest.approx(2.0)

    async def test_counter_reset_clamped(self, plugin, run_cycle):
        _run(plugin, run_cycle, _two_snaps({"sda": (5000, 5000)}, {"sda": (10, 10)}))
        assert _latest_metric("read_kbps") == pytest.approx(0.0)

    async def test_auto_detects_busiest_device(self, plugin, run_cycle):
        result = _run(plugin, run_cycle, _two_snaps(
            {"sda": (0, 0), "sdb": (0, 0)}, {"sda": (2, 0), "sdb": (1000, 0)}))
        assert result.settings[f"disks:{plugin.id}:active_device"] == "sdb"

    async def test_explicit_device_missing_fails(self, make_plugin, run_cycle):
        p = make_plugin(DiskIo, dict(CFG, device='sda'))
        _run(p, run_cycle, _two_snaps({"sdb": (0, 0)}, {"sdb": (2, 0)}))
        assert _latest_status() == "failed"

    async def test_malformed_fails(self, plugin, run_cycle):
        _run(plugin, run_cycle, "no separator here")
        assert _latest_status() == "failed"

    async def test_ssh_failure_fails(self, plugin, run_cycle):
        _run(plugin, run_cycle, "", code=-1, stderr="err")
        assert _latest_status() == "failed"
