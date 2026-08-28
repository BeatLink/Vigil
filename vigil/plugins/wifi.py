"""WiFi link quality and signal strength, read from /proc/net/wireless."""

from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.signal_plugin import (
    LOG_LEVEL as _LOG_LEVEL, SignalPlugin,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig


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


class Wifi(SignalPlugin):
    """WiFi link quality and signal strength from /proc/net/wireless. Its
    thresholds are floors — lower is worse — so it does not use level_for."""

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.interface: Optional[str] = config.get('interface')
        self.quality_warning   = float(config.get('quality_warning',   40))
        self.quality_threshold = float(config.get('quality_threshold', 20))

        from vigil.core.ui.spec import register_color_rule
        self._color_rule = f'wifi_quality_{self.id}'
        register_color_rule(self._color_rule)(self._quality_color)

    @property
    def setting_key(self) -> str:
        return f"network:{self.id}:wifi_interface"

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
        return self.data.get_setting(self.setting_key) or self.interface or 'Detecting...'

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'wifi_iface_card': {'title': 'WIFI INTERFACE', 'value_attr': 'active_interface_text',
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
