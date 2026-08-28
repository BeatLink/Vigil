"""One monitor for a host's networking signals — interface throughput, TCP
connection states and WiFi link quality — instead of one plugin instance per
signal.

Each signal is a module, configured and rendered independently via the
`modules` config block, following the same contract as `system_stats`. Every
module is opt-in — one that isn't declared is off — so a target only collects
(and only shows) what it is asked for. Modules stay pure: each declares its own
commands, parses its own results into metrics/logs/a status, and contributes
its own cards and charts. The plugin concatenates their commands, slices the
results back out positionally, and reports the worst module status as the
overall one.
"""

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.module_plugin import (
    LOG_LEVEL as _LOG_LEVEL, Module as _Module, ModularPlugin, SEVERITY as _SEVERITY,
    module_options, worst_status as _worst,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.plugins.base.plugin_helpers import level_for as _level_for


# ---------------------------------------------------------------------------
# Throughput
# ---------------------------------------------------------------------------

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


class _ThroughputModule(_Module):
    """Interface throughput from two /proc/net/dev samples a second apart,
    taken on the target so the sleep costs one round trip rather than two."""

    key = 'throughput'

    def __init__(self, plugin: 'Network', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.interface: Optional[str] = options.get('interface')

    @property
    def setting_key(self) -> str:
        return f"network:{self.plugin.id}:throughput_interface"

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
        return self.plugin.data.get_setting(self.setting_key) or self.interface or 'Detecting...'

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'iface_card': {'title': 'INTERFACE', 'value_attr': '_throughput_interface',
                           'refresh': True},
            'rx_card': {'metric': 'rx_kbps', 'title': 'DOWNLOAD', 'format': 'kbps_rate'},
            'tx_card': {'metric': 'tx_kbps', 'title': 'UPLOAD', 'format': 'kbps_rate'},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {
            'rx_chart': {'metric': 'rx_kbps', 'title': 'DOWNLOAD HISTORY (KB/s)'},
            'tx_chart': {'metric': 'tx_kbps', 'title': 'UPLOAD HISTORY (KB/s)'},
        }


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

_TCP_STATES = {
    '01': 'ESTABLISHED',
    '02': 'SYN_SENT',
    '03': 'SYN_RECV',
    '04': 'FIN_WAIT1',
    '05': 'FIN_WAIT2',
    '06': 'TIME_WAIT',
    '07': 'CLOSE',
    '08': 'CLOSE_WAIT',
    '09': 'LAST_ACK',
    '0A': 'LISTEN',
    '0B': 'CLOSING',
}


def _parse_states(stdout: str) -> Counter:
    """Count the sockets in each TCP state across the /proc/net/tcp tables."""
    counts: Counter = Counter()
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        if fields[0].rstrip(':') == 'sl':
            continue
        state = _TCP_STATES.get(fields[3].upper())
        if state:
            counts[state] += 1
    return counts


class _ConnectionsModule(_Module):
    """TCP socket counts per state, read straight from /proc/net/tcp{,6} so no
    tooling is required on the target."""

    key = 'connections'

    def __init__(self, plugin: 'Network', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.warning   = int(options.get('warning',   500))
        self.threshold = int(options.get('threshold', 1000))

        from vigil.core.ui.spec import register_color_rule, threshold_color
        self._color_rule = f'network_connections_{plugin.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))

    def commands(self) -> List[Command]:
        return [Command("cat /proc/net/tcp /proc/net/tcp6 2>/dev/null")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/net/tcp: {stderr}")

        counts = _parse_states(stdout)
        total = sum(counts.values())

        metrics = {f'conn_{state.lower()}': float(counts.get(state, 0))
                   for state in _TCP_STATES.values()}
        metrics['conn_total'] = float(total)

        status = _level_for(total, self.warning, self.threshold)
        summary = ', '.join(f"{s}={counts[s]}" for s in sorted(counts)) or "no connections"
        return CollectResult(
            metrics=metrics,
            logs=[(f"{total} TCP connections ({summary})", _LOG_LEVEL[status])],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'conn_total_card': {'metric': 'conn_total', 'title': 'CONNECTIONS',
                                'format': 'int_rounded', 'color': self._color_rule},
            'conn_established_card': {'metric': 'conn_established', 'title': 'ESTABLISHED',
                                      'format': 'int_rounded'},
            'conn_listen_card': {'metric': 'conn_listen', 'title': 'LISTENING',
                                 'format': 'int_rounded'},
            'conn_timewait_card': {'metric': 'conn_time_wait', 'title': 'TIME_WAIT',
                                   'format': 'int_rounded'},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {
            'conn_total_chart': {'metric': 'conn_total', 'title': 'TOTAL CONNECTIONS'},
            'conn_established_chart': {'metric': 'conn_established', 'title': 'ESTABLISHED'},
        }


# ---------------------------------------------------------------------------
# WiFi
# ---------------------------------------------------------------------------


def _parse_wireless(stdout: str) -> Dict[str, Tuple[float, float]]:
    """Map wireless interface name to (link quality, signal dBm)."""
    result: Dict[str, Tuple[float, float]] = {}
    for line in stdout.splitlines():
        if ':' not in line:
            continue
        iface, rest = line.split(':', 1)
        iface = iface.strip()
        fields = rest.split()
        if len(fields) < 3:
            continue
        try:
            link = float(fields[1].rstrip('.'))
            level = float(fields[2].rstrip('.'))
        except (ValueError, IndexError):
            continue
        result[iface] = (link, level)
    return result


def _strongest_interface(stats: Dict[str, Tuple[float, float]]) -> Optional[str]:
    """The wireless interface with the best link quality, if any."""
    return max(stats, key=lambda i: stats[i][0]) if stats else None


class _WifiModule(_Module):
    """WiFi link quality and signal strength from /proc/net/wireless. Its
    thresholds are floors — lower is worse — so it does not use level_for."""

    key = 'wifi'

    def __init__(self, plugin: 'Network', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.interface: Optional[str] = options.get('interface')
        self.quality_warning   = float(options.get('quality_warning',   40))
        self.quality_threshold = float(options.get('quality_threshold', 20))

        from vigil.core.ui.spec import register_color_rule
        self._color_rule = f'network_wifi_quality_{plugin.id}'
        register_color_rule(self._color_rule)(self._quality_color)

    @property
    def setting_key(self) -> str:
        return f"network:{self.plugin.id}:wifi_interface"

    def _level_for_quality(self, quality: float) -> str:
        if quality <= self.quality_threshold:
            return 'failed'
        if quality <= self.quality_warning:
            return 'warning'
        return 'online'

    def _quality_color(self, quality: Optional[float]) -> Optional[str]:
        return None if quality is None else self._level_for_quality(quality)

    def commands(self) -> List[Command]:
        return [Command("cat /proc/net/wireless")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/net/wireless: {stderr}")

        stats = _parse_wireless(stdout)
        iface = self.interface or _strongest_interface(stats)
        if not iface:
            return CollectResult.failed("No wireless interface found")
        if iface not in stats:
            return CollectResult.failed(f"Interface '{iface}' not found in /proc/net/wireless")

        quality, signal = stats[iface]
        status = self._level_for_quality(quality)
        return CollectResult(
            metrics={'link_quality': quality, 'signal_dbm': signal},
            logs=[(f"{iface}: link quality {quality:.0f}, signal {signal:.0f} dBm",
                   _LOG_LEVEL[status])],
            status=status,
            settings={self.setting_key: iface},
        )

    @property
    def active_interface_text(self) -> str:
        return self.plugin.data.get_setting(self.setting_key) or self.interface or 'Detecting...'

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'wifi_iface_card': {'title': 'WIFI INTERFACE', 'value_attr': '_wifi_interface',
                                'refresh': True},
            'quality_card': {'metric': 'link_quality', 'title': 'LINK QUALITY',
                             'format': 'int_rounded', 'color': self._color_rule},
            'signal_card': {'metric': 'signal_dbm', 'title': 'SIGNAL', 'format': 'dbm0'},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {
            'quality_chart': {'metric': 'link_quality', 'title': 'LINK QUALITY'},
            'signal_chart': {'metric': 'signal_dbm', 'title': 'SIGNAL (dBm)'},
        }


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

# Canonical order; also the set of names `modules` may enable.
_MODULE_TYPES = [_ThroughputModule, _ConnectionsModule, _WifiModule]


def _module_options(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Resolve this plugin's `modules` block to {module key: options}."""
    return module_options('network', config, _MODULE_TYPES)


class Network(ModularPlugin):
    MODULE_TYPES = _MODULE_TYPES
    MODULE_LABEL = 'network'

    @property
    def _throughput_interface(self) -> str:
        module = self._module('throughput')
        return module.active_interface_text if module else '--'

    @property
    def _wifi_interface(self) -> str:
        module = self._module('wifi')
        return module.active_interface_text if module else '--'
