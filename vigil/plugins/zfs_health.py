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
    """Dashboard rendering for the zfs_health monitor — declarative, see
    UI_SPEC. ok_card/degraded_card follow the same fixed-online /
    nonzero-failed shape as raid.py and smart_disk.py, registered locally
    here for the same self-containment reason.
    """

    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'total_card': {'metric': 'pools_total', 'title': 'POOLS', 'format': 'int'},
            'ok_card': {
                'metric': 'pools_ok', 'title': 'HEALTHY', 'format': 'int',
                'color': 'zfs_health_always_online',
            },
            'degraded_card': {
                'metric': 'pools_degraded', 'title': 'DEGRADED', 'format': 'int',
                'color': 'zfs_health_nonzero_failed',
            },
        },
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_color_rule


@register_color_rule('zfs_health_always_online')
def _zfs_health_ok_color(v):
    return None if v is None else 'online'


@register_color_rule('zfs_health_nonzero_failed')
def _zfs_health_degraded_color(v):
    if v is None:
        return None
    return 'failed' if v else 'online'
