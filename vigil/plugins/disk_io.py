"""Disk read/write throughput, sampled from /proc/diskstats."""

from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.signal_plugin import (
    SignalPlugin,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig


_SECTOR_BYTES = 512


_VIRTUAL_PREFIXES = ('loop', 'ram', 'dm-', 'sr', 'fd', 'md')


def _parse_diskstats(stdout: str) -> Dict[str, Tuple[int, int]]:
    """Map device name to (sectors read, sectors written) from one /proc/diskstats block."""
    result: Dict[str, Tuple[int, int]] = {}
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        name = fields[2]
        try:
            sectors_read = int(fields[5])
            sectors_written = int(fields[9])
        except (ValueError, IndexError):
            continue
        result[name] = (sectors_read, sectors_written)
    return result


def _is_physical(name: str) -> bool:
    """Whether a device is a whole physical disk rather than a partition or virtual node."""
    if any(name.startswith(p) for p in _VIRTUAL_PREFIXES):
        return False
    if name[-1:].isdigit() and not name.startswith('nvme') and not name.startswith('mmcblk'):
        return False
    if 'p' in name and name.split('p')[-1].isdigit() and (name.startswith('nvme') or name.startswith('mmcblk')):
        return False
    return True


def _auto_detect_device(s1: Dict[str, Tuple[int, int]], s2: Dict[str, Tuple[int, int]]) -> Optional[str]:
    """The busiest physical disk across the two snapshots, or the first one when all are idle."""
    activity = {}
    for name in s1:
        if name not in s2 or not _is_physical(name):
            continue
        r = (s2[name][0] - s1[name][0]) + (s2[name][1] - s1[name][1])
        activity[name] = r
    if not activity:
        physical = [n for n in s1 if _is_physical(n)]
        return physical[0] if physical else None
    return max(activity, key=activity.__getitem__)


def _format_rate(kbps: float) -> str:
    """A KB/s rate rendered as a human-readable throughput string."""
    if kbps >= 1024:
        return f"{kbps / 1024:.1f} MB/s"
    return f"{kbps:.1f} KB/s"


class DiskIo(SignalPlugin):
    """Read and write throughput for one disk, from two /proc/diskstats samples
    a second apart taken on the target so the sleep costs one round trip."""

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.device: Optional[str] = config.get('device')

    @property
    def setting_key(self) -> str:
        return f"disks:{self.id}:active_device"

    SAMPLED = True

    def commands(self) -> List[Command]:
        return [Command("cat /proc/diskstats && sleep 1 && echo '---SNAP---' && cat /proc/diskstats")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/diskstats: {stderr}")

        halves = stdout.split('---SNAP---')
        if len(halves) < 2:
            return CollectResult.failed("Unexpected /proc/diskstats output format")

        s1 = _parse_diskstats(halves[0])
        s2 = _parse_diskstats(halves[1])

        device = self.device or _auto_detect_device(s1, s2)
        if not device:
            return CollectResult.failed("No usable disk device found")

        if device not in s1 or device not in s2:
            return CollectResult.failed(f"Device '{device}' not found in /proc/diskstats")

        read_kbps = max(0.0, (s2[device][0] - s1[device][0]) * _SECTOR_BYTES / 1024)
        write_kbps = max(0.0, (s2[device][1] - s1[device][1]) * _SECTOR_BYTES / 1024)

        return CollectResult(
            metrics={'read_kbps': read_kbps, 'write_kbps': write_kbps},
            logs=[(
                f"Disk {device}: read {_format_rate(read_kbps)}, write {_format_rate(write_kbps)}",
                "INFO",
            )],
            status='online',
            settings={self.setting_key: device},
        )

    @property
    def active_device_text(self) -> str:
        return self.data.get_setting(self.setting_key) or self.device or 'Detecting...'

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'io_device_card': {'title': 'DEVICE', 'value_attr': 'active_device_text', 'refresh': True},
            'read_card': {'metric': 'read_kbps', 'title': 'READ', 'format': 'kbps_rate'},
            'write_card': {'metric': 'write_kbps', 'title': 'WRITE', 'format': 'kbps_rate'},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {
            'read_chart': {'metric': 'read_kbps', 'title': 'READ THROUGHPUT (KB/s)'},
            'write_chart': {'metric': 'write_kbps', 'title': 'WRITE THROUGHPUT (KB/s)'},
        }
