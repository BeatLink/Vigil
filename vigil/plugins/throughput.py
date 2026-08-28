"""Network interface throughput, sampled from /proc/net/dev."""

from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.signal_plugin import (
    SignalPlugin,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig


_VIRTUAL_PREFIXES = ('lo', 'veth', 'docker', 'virbr', 'br-', 'tun', 'tap')


def _parse_net_dev(block: str) -> Dict[str, Tuple[int, int]]:
    """Map interface name to (rx bytes, tx bytes) from one /proc/net/dev block."""
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


def _busiest_interface(stats: Dict[str, Tuple[int, int]]) -> Optional[str]:
    """The non-virtual interface with the most cumulative traffic, if any."""
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


class Throughput(SignalPlugin):
    """Interface throughput from two /proc/net/dev samples a second apart,
    taken on the target so the sleep costs one round trip rather than two."""

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.interface: Optional[str] = config.get('interface')

    @property
    def setting_key(self) -> str:
        return f"network:{self.id}:throughput_interface"

    def commands(self) -> List[Command]:
        return [Command("cat /proc/net/dev && sleep 1 && cat /proc/net/dev")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/net/dev: {stderr}")

        halves = stdout.split('Inter-|')
        if len(halves) < 3:
            return CollectResult.failed("Unexpected /proc/net/dev output format")

        sample1 = _parse_net_dev(halves[1])
        sample2 = _parse_net_dev(halves[2])

        iface = self.interface or _busiest_interface(sample1)
        if not iface:
            return CollectResult.failed("No usable network interface found")
        if iface not in sample1 or iface not in sample2:
            return CollectResult.failed(f"Interface '{iface}' not found in /proc/net/dev")

        rx1, tx1 = sample1[iface]
        rx2, tx2 = sample2[iface]
        rx_kbps = max(0.0, (rx2 - rx1) / 1024)
        tx_kbps = max(0.0, (tx2 - tx1) / 1024)

        return CollectResult(
            metrics={'rx_kbps': rx_kbps, 'tx_kbps': tx_kbps},
            logs=[(
                f"Interface {iface}: RX {_format_rate(rx_kbps)}, TX {_format_rate(tx_kbps)}",
                "INFO",
            )],
            status='online',
            settings={self.setting_key: iface},
        )

    @property
    def active_interface_text(self) -> str:
        return self.data.get_setting(self.setting_key) or self.interface or 'Detecting...'

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'iface_card': {'title': 'INTERFACE', 'value_attr': 'active_interface_text',
                           'refresh': True},
            'rx_card': {'metric': 'rx_kbps', 'title': 'DOWNLOAD', 'format': 'kbps_rate'},
            'tx_card': {'metric': 'tx_kbps', 'title': 'UPLOAD', 'format': 'kbps_rate'},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {
            'rx_chart': {'metric': 'rx_kbps', 'title': 'DOWNLOAD HISTORY (KB/s)'},
            'tx_chart': {'metric': 'tx_kbps', 'title': 'UPLOAD HISTORY (KB/s)'},
        }
