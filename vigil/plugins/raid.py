import re
from typing import Dict, Any

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

# /proc/mdstat lays out one array per md device. The line after the "mdN :"
# header carries a status field like "[4/4] [UUUU]" — each U is an up disk,
# each _ a missing/failed one. A resync/recovery line may follow.
_ARRAY_RE = re.compile(r'^(md\d+)\s*:\s*(\S+)\s+(\S+)', re.MULTILINE)
_STATE_RE = re.compile(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]')
_RECOVERY_RE = re.compile(r'(recovery|resync|reshape|check)\s*=\s*([\d.]+)%')

_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'ok_card', 'degraded_card'],
    ['arrays'],
    ['events'],
]


class RaidCollectorPlugin(CollectorPlugin):
    """
    Monitors Linux software RAID (mdadm) array health over SSH via /proc/mdstat.

    Parses each md device's [N/M] [UU__] status. An array is degraded when
    fewer disks are up than the array expects, or when any slot shows '_'.
    Reports failed if any array is degraded, warning while a resync/recovery is
    in progress, online when all arrays are clean. Complements the ZFS plugins
    for hosts using classic mdraid.

    No config options beyond the shared SSH/interval fields.
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output("cat /proc/mdstat 2>&1")

        if ret != 0 and not stdout.strip():
            self.db_logger.write(f"Failed to read /proc/mdstat: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        ok = degraded = 0
        recovering = False

        for m in _ARRAY_RE.finditer(stdout):
            dev = m.group(1)
            # The status/state line is the remainder of this array block up to a
            # blank line; search the slice after the header for the [N/M][U_] tag.
            block = stdout[m.end():]
            next_blank = block.find('\n\n')
            block = block if next_blank < 0 else block[:next_blank]

            state = _STATE_RE.search(block)
            recov = _RECOVERY_RE.search(block)

            if state:
                expected, active, flags = int(state.group(1)), int(state.group(2)), state.group(3)
                down = flags.count('_')
                if down > 0 or active < expected:
                    degraded += 1
                    self.db_logger.write(
                        f"{dev}: DEGRADED [{active}/{expected}] [{flags}]", level="ERROR"
                    )
                    continue

            if recov:
                recovering = True
                self.db_logger.write(
                    f"{dev}: {recov.group(1)} {recov.group(2)}% in progress", level="WARNING"
                )
                ok += 1
                continue

            ok += 1
            self.db_logger.write(f"{dev}: clean", level="INFO")

        total = ok + degraded
        if total == 0:
            self.db_logger.write("No RAID arrays found in /proc/mdstat", level="WARNING")
            self.set_status('offline')
            return

        self.db_metrics.metric('arrays_total', float(total))
        self.db_metrics.metric('arrays_ok', float(ok))
        self.db_metrics.metric('arrays_degraded', float(degraded))

        if degraded > 0:
            self.set_status('failed')
        elif recovering:
            self.set_status('warning')
        else:
            self.set_status('online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class RaidUIPlugin(UIPlugin):
    """Dashboard rendering for the raid monitor."""

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['arrays_total', 'arrays_ok', 'arrays_degraded'])

        def _int_or_dash(v):
            return '--' if v is None else str(int(v))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('total_card'):
            info_card('ARRAYS', '--').bind_text_from(
                page.model, ('metrics', 'arrays_total'), backward=_int_or_dash)
        with layout.cell('ok_card'):
            info_card('CLEAN', '--').bind_text_from(
                page.model, ('metrics', 'arrays_ok'), backward=_int_or_dash
            ).style(f"color: {STATUS_COLORS['online']}")
        with layout.cell('degraded_card'):
            degraded_label = info_card('DEGRADED', '--').bind_text_from(
                page.model, ('metrics', 'arrays_degraded'), backward=_int_or_dash)
        with layout.cell('arrays'):
            ui.element('div')  # reserved for future per-array detail
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_color():
            degraded = page.model.metrics.get('arrays_degraded')
            if degraded is not None:
                color = STATUS_COLORS['failed'] if degraded else STATUS_COLORS['online']
                degraded_label.style(f"color: {color}")

        page.on_refresh(update_color)
        page.start()
