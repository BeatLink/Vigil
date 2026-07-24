from typing import Dict, Any, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for


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


class WifiCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.interface: Optional[str] = config.get('interface')
        self.quality_warning   = float(config.get('quality_warning',   40))
        self.quality_threshold = float(config.get('quality_threshold', 20))
        self._active_interface: Optional[str] = self.interface

    def _level_for_quality(self, quality: float) -> str:
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


class WifiUIPlugin(UIPlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interface: Optional[str] = self.config.get('interface')
        self.quality_warning   = float(self.config.get('quality_warning',   40))
        self.quality_threshold = float(self.config.get('quality_threshold', 20))
        self._active_interface: Optional[str] = self.interface

    def _level_for_quality(self, quality: float) -> str:
        if quality <= self.quality_threshold:
            return 'failed'
        if quality <= self.quality_warning:
            return 'warning'
        return 'online'

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['link_quality', 'signal_dbm'])

        def _quality_or_dash(v):
            return '--' if v is None else f'{v:.0f}'

        def _signal_or_dash(v):
            return '-- dBm' if v is None else f'{v:.0f} dBm'

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('iface_card'):
            iface_label = info_card('INTERFACE', self._active_interface or 'Detecting...')
        with layout.cell('quality_card'):
            quality_label = info_card('LINK QUALITY', '--').bind_text_from(
                page.model, ('metrics', 'link_quality'), backward=_quality_or_dash)
        with layout.cell('signal_card'):
            info_card('SIGNAL', '-- dBm').bind_text_from(
                page.model, ('metrics', 'signal_dbm'), backward=_signal_or_dash)
        with layout.cell('quality_chart'):
            history_chart(page, 'LINK QUALITY', self.id, 'link_quality')
        with layout.cell('signal_chart'):
            history_chart(page, 'SIGNAL (dBm)', self.id, 'signal_dbm')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_iface_and_color():
            if self._active_interface:
                iface_label.text = self._active_interface
            quality = page.model.metrics.get('link_quality')
            if quality is not None:
                quality_label.style(f'color: {STATUS_COLORS[self._level_for_quality(quality)]}')

        page.on_refresh(update_iface_and_color)

        page.start()
