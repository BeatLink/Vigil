from typing import Dict, Any

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

# Pool health states that indicate a problem
_UNHEALTHY = {'DEGRADED', 'FAULTED', 'OFFLINE', 'UNAVAIL', 'REMOVED'}

_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'ok_card', 'degraded_card'],
    ['events'],
]


class ZFSHealthCollectorPlugin(CollectorPlugin):
    """
    Monitors ZFS pool health states over SSH.
    Checks all pools via 'zpool list -H -o name,health' and reports failed
    if any pool is in a DEGRADED, FAULTED, OFFLINE, UNAVAIL, or REMOVED state.
    This complements zfs_pool (capacity monitoring) with structural integrity checks.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            "zpool list -H -o name,health 2>&1"
        )

        if ret != 0 and not stdout.strip():
            self.db_logger.write(f"zpool list failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        ok, degraded = 0, 0
        for line in stdout.splitlines():
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            pool, health = parts
            if health in _UNHEALTHY:
                degraded += 1
                self.db_logger.write(f"Pool {pool}: {health}", level="ERROR")
            else:
                ok += 1
                self.db_logger.write(f"Pool {pool}: {health}", level="INFO")

        total = ok + degraded
        if total == 0:
            self.db_logger.write("No ZFS pools found", level="WARNING")
            self.set_status('offline')
            return

        self.db_metrics.metric("pools_total", total)
        self.db_metrics.metric("pools_ok", ok)
        self.db_metrics.metric("pools_degraded", degraded)
        self.set_status('failed' if degraded > 0 else 'online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class ZFSHealthUIPlugin(UIPlugin):
    """Dashboard rendering for the zfs_health monitor."""

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.web.ui.theme import STATUS_COLORS
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['pools_total', 'pools_ok', 'pools_degraded'])

        def _int_or_dash(v):
            return '--' if v is None else str(int(v))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('total_card'):
            info_card('POOLS', '--').bind_text_from(
                page.model, ('metrics', 'pools_total'), backward=_int_or_dash)
        with layout.cell('ok_card'):
            ok_label = info_card('HEALTHY', '--').bind_text_from(
                page.model, ('metrics', 'pools_ok'), backward=_int_or_dash
            ).style(f"color: {STATUS_COLORS['online']}")
        with layout.cell('degraded_card'):
            degraded_label = info_card('DEGRADED', '--').bind_text_from(
                page.model, ('metrics', 'pools_degraded'), backward=_int_or_dash)
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_degraded_color():
            degraded = page.model.metrics.get('pools_degraded')
            if degraded is not None:
                color = STATUS_COLORS['failed'] if degraded else STATUS_COLORS['online']
                degraded_label.style(f"color: {color}")

        page.on_refresh(update_degraded_color)

        page.start()
