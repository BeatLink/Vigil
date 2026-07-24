from typing import Dict, Any, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

_VIRTUAL_PREFIXES = ('lo', 'veth', 'docker', 'virbr', 'br-', 'tun', 'tap')


def _parse_net_dev(block: str) -> Dict[str, Tuple[int, int]]:
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


_DEFAULT_LAYOUT = [
    ['host_card', 'iface_card', 'rx_card', 'tx_card'],
    ['rx_chart'],
    ['tx_chart'],
    ['events'],
]


class NetworkUsageCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.interface: Optional[str] = config.get('interface')
        self._active_interface: Optional[str] = self.interface

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            "cat /proc/net/dev && sleep 1 && cat /proc/net/dev"
        )
        if ret != 0:
            self.db_logger.write(f"Failed to read /proc/net/dev: {stderr}", level="ERROR")
            self.set_status('failed')
            return

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


class NetworkUsageUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.spec import FORMATTERS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['rx_kbps', 'tx_kbps'])

        configured_interface = self.config.get('interface')
        _rate_or_dash = FORMATTERS['kbps_rate']

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('iface_card'):
            info_card('INTERFACE', configured_interface or 'Detecting...')
        with layout.cell('rx_card'):
            info_card('DOWNLOAD', '-- KB/s').bind_text_from(
                page.model, ('metrics', 'rx_kbps'), backward=_rate_or_dash)
        with layout.cell('tx_card'):
            info_card('UPLOAD', '-- KB/s').bind_text_from(
                page.model, ('metrics', 'tx_kbps'), backward=_rate_or_dash)
        with layout.cell('rx_chart'):
            history_chart(page, 'DOWNLOAD HISTORY (KB/s)', self.id, 'rx_kbps')
        with layout.cell('tx_chart'):
            history_chart(page, 'UPLOAD HISTORY (KB/s)', self.id, 'tx_kbps')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        page.start()
