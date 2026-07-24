from typing import Dict, Any, List, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
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
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.interface: Optional[str] = config.get('interface')
        self.quality_warning   = float(config.get('quality_warning',   40))
        self.quality_threshold = float(config.get('quality_threshold', 20))

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


class WifiUIPlugin(UIPlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interface: Optional[str] = self.config.get('interface')
        self.quality_warning   = float(self.config.get('quality_warning',   40))
        self.quality_threshold = float(self.config.get('quality_threshold', 20))

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
        page = self.ui.page(metric_names=['link_quality', 'signal_dbm'])

        active_interface = self.storage.get_setting(f"wifi:{self.id}:active_interface") or self.interface

        def _quality_or_dash(v):
            return '--' if v is None else f'{v:.0f}'

        def _signal_or_dash(v):
            return '-- dBm' if v is None else f'{v:.0f} dBm'

        with layout.cell('host_card'):
            self.ui.host_card()
        with layout.cell('iface_card'):
            iface_label = info_card('INTERFACE', active_interface or 'Detecting...')
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
            self.ui.events_table(page)

        def update_iface_and_color():
            device = self.storage.get_setting(f"wifi:{self.id}:active_interface")
            if device:
                iface_label.text = device
            quality = page.model.metrics.get('link_quality')
            if quality is not None:
                quality_label.style(f'color: {STATUS_COLORS[self._level_for_quality(quality)]}')

        page.on_refresh(update_iface_and_color)

        page.start()
