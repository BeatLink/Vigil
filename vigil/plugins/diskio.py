from typing import Dict, Any, Optional, Tuple

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart, safe_timer

# Sectors are a fixed 512 bytes in /proc/diskstats regardless of physical
# sector size — this is a kernel ABI constant, not the device geometry.
_SECTOR_BYTES = 512

# Device-name prefixes treated as virtual/non-physical for auto-detection.
_VIRTUAL_PREFIXES = ('loop', 'ram', 'dm-', 'sr', 'fd', 'md')


def _parse_diskstats(stdout: str) -> Dict[str, Tuple[int, int]]:
    """Parse /proc/diskstats into {device: (sectors_read, sectors_written)}.

    Columns: major minor name reads_completed reads_merged sectors_read
    time_reading writes_completed writes_merged sectors_written ...
    (1-indexed by the kernel doc; sectors_read is field 6, sectors_written 10).
    """
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
    """True if the device looks like a whole physical disk (not a partition)."""
    if any(name.startswith(p) for p in _VIRTUAL_PREFIXES):
        return False
    # Exclude partitions: sdaN, nvme0n1pN, mmcblk0pN.
    if name[-1:].isdigit() and not name.startswith('nvme') and not name.startswith('mmcblk'):
        return False
    if 'p' in name and name.split('p')[-1].isdigit() and (name.startswith('nvme') or name.startswith('mmcblk')):
        return False
    return True


def _auto_detect_device(s1: Dict[str, Tuple[int, int]], s2: Dict[str, Tuple[int, int]]) -> Optional[str]:
    """Return the physical device with the most I/O activity between snapshots."""
    activity = {}
    for name in s1:
        if name not in s2 or not _is_physical(name):
            continue
        r = (s2[name][0] - s1[name][0]) + (s2[name][1] - s1[name][1])
        activity[name] = r
    if not activity:
        # No activity delta — fall back to any physical device present.
        physical = [n for n in s1 if _is_physical(n)]
        return physical[0] if physical else None
    return max(activity, key=activity.__getitem__)


def _format_rate(kbps: float) -> str:
    if kbps >= 1024:
        return f"{kbps / 1024:.1f} MB/s"
    return f"{kbps:.1f} KB/s"


_DEFAULT_LAYOUT = [
    ['host_card', 'device_card', 'read_card', 'write_card'],
    ['read_chart'],
    ['write_chart'],
    ['events'],
]


class DiskIoPlugin(BasePlugin):
    """
    Monitors per-disk read/write throughput over SSH via /proc/diskstats.

    Takes two snapshots 1 second apart in a single SSH command and converts the
    sector deltas to KB/s — no iostat or extra tools required on the target.

    Config options:
      device   (optional) explicit device name, e.g. "sda" or "nvme0n1".
               Omit to auto-detect the busiest physical disk.
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.device: Optional[str] = config.get('device')
        self._active_device: Optional[str] = self.device

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            "cat /proc/diskstats && sleep 1 && echo '---SNAP---' && cat /proc/diskstats"
        )
        if ret != 0:
            self.db_logger.write(f"Failed to read /proc/diskstats: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        halves = stdout.split('---SNAP---')
        if len(halves) < 2:
            self.db_logger.write("Unexpected /proc/diskstats output format", level="ERROR")
            self.set_status('failed')
            return

        s1 = _parse_diskstats(halves[0])
        s2 = _parse_diskstats(halves[1])

        device = self.device or _auto_detect_device(s1, s2)
        if not device:
            self.db_logger.write("No usable disk device found", level="ERROR")
            self.set_status('failed')
            return

        if device not in s1 or device not in s2:
            self.db_logger.write(f"Device '{device}' not found in /proc/diskstats", level="ERROR")
            self.set_status('failed')
            return

        read_kbps = max(0.0, (s2[device][0] - s1[device][0]) * _SECTOR_BYTES / 1024)
        write_kbps = max(0.0, (s2[device][1] - s1[device][1]) * _SECTOR_BYTES / 1024)

        self._active_device = device
        self.db_metrics.metric('read_kbps', read_kbps)
        self.db_metrics.metric('write_kbps', write_kbps)
        self.db_logger.write(
            f"Disk {device}: read {_format_rate(read_kbps)}, write {_format_rate(write_kbps)}",
            level="INFO"
        )
        self.set_status('online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('device_card'):
            device_label = info_card('DEVICE', self._active_device or 'Detecting...')
        with layout.cell('read_card'):
            read_label = info_card('READ', '-- KB/s')
        with layout.cell('write_card'):
            write_label = info_card('WRITE', '-- KB/s')
        with layout.cell('read_chart'):
            history_chart('READ THROUGHPUT (KB/s)', self.id, 'read_kbps')
        with layout.cell('write_chart'):
            history_chart('WRITE THROUGHPUT (KB/s)', self.id, 'write_kbps')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            if self._active_device:
                device_label.text = self._active_device
            read = self.latest_metric('read_kbps')
            write = self.latest_metric('write_kbps')
            if read:
                read_label.text = _format_rate(read.value)
            if write:
                write_label.text = _format_rate(write.value)

        safe_timer(5.0, update_cards)
