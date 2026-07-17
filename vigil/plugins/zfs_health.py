from typing import Dict, Any
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, safe_timer

# Pool health states that indicate a problem
_UNHEALTHY = {'DEGRADED', 'FAULTED', 'OFFLINE', 'UNAVAIL', 'REMOVED'}

_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'ok_card', 'degraded_card'],
    ['logs'],
]


class ZFSHealthPlugin(BasePlugin):
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

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.core.ui.theme import STATUS_COLORS
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('total_card'):
            total_label = info_card('POOLS', '--')
        with layout.cell('ok_card'):
            ok_label = info_card('HEALTHY', '--')
        with layout.cell('degraded_card'):
            degraded_label = info_card('DEGRADED', '--')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_cards():
            def _ival(name):
                m = self.latest_metric(name)
                return int(m.value) if m else None

            total = _ival('pools_total')
            ok = _ival('pools_ok')
            degraded = _ival('pools_degraded')
            if total is not None:
                total_label.text = str(total)
                ok_label.text = str(ok)
                ok_label.style(f"color: {STATUS_COLORS['online']}")
                degraded_label.text = str(degraded)
                color = STATUS_COLORS['failed'] if degraded else STATUS_COLORS['online']
                degraded_label.style(f"color: {color}")

        safe_timer(5.0, update_cards)
