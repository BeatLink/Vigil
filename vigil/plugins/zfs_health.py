from typing import Dict, Any
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card

# Pool health states that indicate a problem
_UNHEALTHY = {'DEGRADED', 'FAULTED', 'OFFLINE', 'UNAVAIL', 'REMOVED'}


class ZFSHealthPlugin(BasePlugin):
    """
    Monitors ZFS pool health states over SSH.
    Checks all pools via 'zpool list -H -o name,health' and reports failed
    if any pool is in a DEGRADED, FAULTED, OFFLINE, UNAVAIL, or REMOVED state.
    This complements zfs_pool (capacity monitoring) with structural integrity checks.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.ssh_collector = self.internal_modules['collectors'].get('ssh')
        self.db_logger = self.internal_modules['loggers'].get('db_logs')
        self.db_metrics = self.internal_modules['loggers'].get('db_metrics')

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

    def render_ui(self):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.core.ui.theme import STATUS_COLORS

        with ui.row().classes('w-full gap-4 mb-4'):
            self.internal_modules['ui']['host_card']()

            total_label = info_card('POOLS', '--')
            ok_label = info_card('HEALTHY', '--')
            degraded_label = info_card('DEGRADED', '--')

            def update_cards():
                def latest(metric):
                    m = Metric.select().where(
                        (Metric.collector == self.name) & (Metric.metric_name == metric)
                    ).order_by(Metric.timestamp.desc()).first()
                    return int(m.value) if m else None

                total = latest('pools_total')
                ok = latest('pools_ok')
                degraded = latest('pools_degraded')
                if total is not None:
                    total_label.text = str(total)
                    ok_label.text = str(ok)
                    ok_label.style(f"color: {STATUS_COLORS['online']}")
                    degraded_label.text = str(degraded)
                    color = STATUS_COLORS['failed'] if degraded else STATUS_COLORS['online']
                    degraded_label.style(f"color: {color}")

            ui.timer(5.0, update_cards)

        self.internal_modules['ui']['logs_table']()
