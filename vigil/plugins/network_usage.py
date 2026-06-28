import logging
from typing import Dict, Any, Optional, Tuple

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart

# Interface prefixes treated as virtual/internal — excluded from auto-detection
_VIRTUAL_PREFIXES = ('lo', 'veth', 'docker', 'virbr', 'br-', 'tun', 'tap')


def _parse_net_dev(block: str) -> Dict[str, Tuple[int, int]]:
    """Parse one /proc/net/dev block into {iface: (rx_bytes, tx_bytes)}."""
    result = {}
    for line in block.splitlines():
        line = line.strip()
        if ':' not in line:
            continue
        iface, rest = line.split(':', 1)
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            result[iface.strip()] = (int(fields[0]), int(fields[8]))
        except (ValueError, IndexError):
            continue
    return result


def _auto_detect_interface(stats: Dict[str, Tuple[int, int]]) -> Optional[str]:
    """Return the non-virtual interface with the highest combined byte count."""
    candidates = {
        iface: rx + tx
        for iface, (rx, tx) in stats.items()
        if not any(iface.startswith(p) for p in _VIRTUAL_PREFIXES)
    }
    return max(candidates, key=candidates.__getitem__) if candidates else None


def _format_rate(kbps: float) -> str:
    if kbps >= 1024:
        return f"{kbps / 1024:.1f} MB/s"
    return f"{kbps:.1f} KB/s"


class NetworkUsagePlugin(BasePlugin):
    """
    Monitors network interface throughput over SSH via /proc/net/dev.

    Takes two snapshots 1 second apart in a single SSH command to compute
    RX/TX rates without requiring any extra tools on the remote host.

    Config options:
      interface: (optional) explicit interface name, e.g. "eth0".
                 Omit to auto-detect the busiest non-virtual interface.
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.interface: Optional[str] = config.get('interface')
        self._active_interface: Optional[str] = self.interface
        self.ssh_collector = self.internal_modules['collectors']['ssh']
        self.db_logger = self.internal_modules['loggers']['db_logs']
        self.db_metrics = self.internal_modules['loggers']['db_metrics']

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            "cat /proc/net/dev && sleep 1 && cat /proc/net/dev"
        )
        if ret != 0:
            self.db_logger.write(f"Failed to read /proc/net/dev: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        # stdout contains two copies of /proc/net/dev separated by the repeated header
        halves = stdout.split('Inter-|')
        if len(halves) < 3:
            self.db_logger.write("Unexpected /proc/net/dev output format", level="ERROR")
            self.set_status('failed')
            return

        sample1 = _parse_net_dev(halves[1])
        sample2 = _parse_net_dev(halves[2])

        iface = self.interface or _auto_detect_interface(sample1)
        if not iface:
            self.db_logger.write("No usable network interface found", level="ERROR")
            self.set_status('failed')
            return

        if iface not in sample1 or iface not in sample2:
            self.db_logger.write(f"Interface '{iface}' not found in /proc/net/dev", level="ERROR")
            self.set_status('failed')
            return

        rx1, tx1 = sample1[iface]
        rx2, tx2 = sample2[iface]

        # Clamp to 0 to guard against counter resets between the two samples
        rx_kbps = max(0.0, (rx2 - rx1) / 1024)
        tx_kbps = max(0.0, (tx2 - tx1) / 1024)

        self._active_interface = iface
        self.db_metrics.metric('rx_kbps', rx_kbps)
        self.db_metrics.metric('tx_kbps', tx_kbps)
        self.db_logger.write(
            f"Interface {iface}: RX {_format_rate(rx_kbps)}, TX {_format_rate(tx_kbps)}",
            level="INFO"
        )
        self.set_status('online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self):
        from nicegui import ui
        from vigil.core.data.database import Metric

        def latest(metric_name):
            return Metric.select().where(
                (Metric.collector == self.name) & (Metric.metric_name == metric_name)
            ).order_by(Metric.timestamp.desc()).first()

        with ui.row().classes('w-full gap-4 mb-4'):
            self.internal_modules['ui']['host_card']()
            iface_label = info_card('INTERFACE', self._active_interface or 'Detecting...')
            rx_label = info_card('DOWNLOAD', '-- KB/s')
            tx_label = info_card('UPLOAD', '-- KB/s')

            def update_cards():
                if self._active_interface:
                    iface_label.text = self._active_interface
                rx = latest('rx_kbps')
                tx = latest('tx_kbps')
                if rx:
                    rx_label.text = _format_rate(rx.value)
                if tx:
                    tx_label.text = _format_rate(tx.value)
            ui.timer(5.0, update_cards)

        history_chart('DOWNLOAD HISTORY (KB/s)', self.name, 'rx_kbps')
        history_chart('UPLOAD HISTORY (KB/s)', self.name, 'tx_kbps')
        self.internal_modules['ui']['logs_table']()
