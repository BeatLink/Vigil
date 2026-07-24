from typing import Dict, Any, List, Optional, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult


def _parse_wireless(stdout: str) -> Dict[str, Tuple[float, float]]:
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


def _auto_detect_interface(stats: Dict[str, Tuple[float, float]]) -> Optional[str]:
    if not stats:
        return None
    return max(stats, key=lambda i: stats[i][0])


_DEFAULT_LAYOUT = [
    ['host_card', 'iface_card', 'quality_card', 'signal_card'],
    ['quality_chart'],
    ['signal_chart'],
    ['events'],
]


class Wifi(Plugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.interface: Optional[str] = config.get('interface')
        self.quality_warning   = float(config.get('quality_warning',   40))
        self.quality_threshold = float(config.get('quality_threshold', 20))

        from vigil.core.ui.spec import register_color_rule
        self._quality_color_name = f'wifi_quality_{self.id}'
        register_color_rule(self._quality_color_name)(self._quality_color)

    def _level_for_quality(self, quality: float) -> str:
        if quality <= self.quality_threshold:
            return 'failed'
        if quality <= self.quality_warning:
            return 'warning'
        return 'online'

    def commands(self) -> List[Command]:
        return [Command("cat /proc/net/wireless")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/net/wireless: {stderr}")

        stats = _parse_wireless(stdout)
        iface = self.interface or _auto_detect_interface(stats)
        if not iface:
            return CollectResult.failed("No wireless interface found")

        if iface not in stats:
            return CollectResult.failed(f"Interface '{iface}' not found in /proc/net/wireless")

        quality, signal = stats[iface]
        overall = self._level_for_quality(quality)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        return CollectResult(
            metrics={'link_quality': quality, 'signal_dbm': signal},
            logs=[(f"{iface}: link quality {quality:.0f}, signal {signal:.0f} dBm", log_level)],
            status=overall,
            settings={f"wifi:{self.id}:active_interface": iface},
        )

    def _quality_color(self, quality: Optional[float]) -> Optional[str]:
        if quality is None:
            return None
        if quality <= self.quality_threshold:
            return 'failed'
        if quality <= self.quality_warning:
            return 'warning'
        return 'online'

    @property
    def _active_interface_text(self) -> str:
        return self.storage.get_setting(f"wifi:{self.id}:active_interface") or self.interface or 'Detecting...'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'iface_card': {'title': 'INTERFACE', 'value_attr': '_active_interface_text', 'refresh': True},
                'quality_card': {'metric': 'link_quality', 'title': 'LINK QUALITY', 'format': 'int_rounded',
                                 'color': self._quality_color_name},
                'signal_card': {'metric': 'signal_dbm', 'title': 'SIGNAL', 'format': 'dbm0'},
            },
            'charts': {
                'quality_chart': {'metric': 'link_quality', 'title': 'LINK QUALITY'},
                'signal_chart': {'metric': 'signal_dbm', 'title': 'SIGNAL (dBm)'},
            },
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)
