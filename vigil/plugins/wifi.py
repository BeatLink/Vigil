from typing import Dict, Any, Optional, Tuple

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.plugin_utils import level_for as _level_for
from vigil.core.ui.components import info_card, history_chart, safe_timer
from vigil.core.ui.theme import STATUS_COLORS


def _parse_wireless(stdout: str) -> Dict[str, Tuple[float, float]]:
    """Parse /proc/net/wireless into {iface: (link_quality, signal_dbm)}.

    Columns (after the two header lines) are:
      iface: status  link  level  noise  ...
    `link` is the link quality and `level` is the signal level in dBm. Both
    fields often carry a trailing '.' in the file, which we strip.
    """
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
    """Return the wireless interface with the strongest link quality."""
    if not stats:
        return None
    return max(stats, key=lambda i: stats[i][0])


_DEFAULT_LAYOUT = [
    ['host_card', 'iface_card', 'quality_card', 'signal_card'],
    ['quality_chart'],
    ['signal_chart'],
    ['logs'],
]


class WifiPlugin(BasePlugin):
    """
    Monitors WiFi link quality and signal strength over SSH via
    /proc/net/wireless — no wireless tools required on the remote host.

    Status is derived from link quality (a 0-70 scale on most drivers):
    lower quality is worse, so the warning/critical bounds are treated as
    floors — quality below `quality_warning` warns, below `quality_threshold`
    fails.

    Config options:
      interface          (optional) explicit wireless iface, e.g. "wlan0".
                         Omit to auto-detect the strongest link.
      quality_warning    Link quality that triggers warning (default: 40)
      quality_threshold  Link quality that triggers failed  (default: 20)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.interface: Optional[str] = config.get('interface')
        self.quality_warning   = float(config.get('quality_warning',   40))
        self.quality_threshold = float(config.get('quality_threshold', 20))
        self._active_interface: Optional[str] = self.interface

    def _level_for_quality(self, quality: float) -> str:
        """Lower quality is worse, so treat the bounds as floors."""
        if quality <= self.quality_threshold:
            return 'failed'
        if quality <= self.quality_warning:
            return 'warning'
        return 'online'

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            "cat /proc/net/wireless"
        )
        if ret != 0:
            self.db_logger.write(f"Failed to read /proc/net/wireless: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        stats = _parse_wireless(stdout)
        iface = self.interface or _auto_detect_interface(stats)
        if not iface:
            self.db_logger.write("No wireless interface found", level="ERROR")
            self.set_status('failed')
            return

        if iface not in stats:
            self.db_logger.write(f"Interface '{iface}' not found in /proc/net/wireless", level="ERROR")
            self.set_status('failed')
            return

        quality, signal = stats[iface]
        self._active_interface = iface
        self.db_metrics.metric('link_quality', quality)
        self.db_metrics.metric('signal_dbm', signal)

        overall = self._level_for_quality(quality)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        self.db_logger.write(
            f"{iface}: link quality {quality:.0f}, signal {signal:.0f} dBm",
            level=log_level
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('iface_card'):
            iface_label = info_card('INTERFACE', self._active_interface or 'Detecting...')
        with layout.cell('quality_card'):
            quality_label = info_card('LINK QUALITY', '--')
        with layout.cell('signal_card'):
            signal_label = info_card('SIGNAL', '-- dBm')
        with layout.cell('quality_chart'):
            history_chart('LINK QUALITY', self.name, 'link_quality')
        with layout.cell('signal_chart'):
            history_chart('SIGNAL (dBm)', self.name, 'signal_dbm')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_cards():
            if self._active_interface:
                iface_label.text = self._active_interface
            quality = self.latest_metric('link_quality')
            signal = self.latest_metric('signal_dbm')
            if quality:
                quality_label.text = f'{quality.value:.0f}'
                quality_label.style(f'color: {STATUS_COLORS[self._level_for_quality(quality.value)]}')
            if signal:
                signal_label.text = f'{signal.value:.0f} dBm'

        safe_timer(5.0, update_cards)
