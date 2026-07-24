from typing import Dict, Any, List

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin

_UNHEALTHY = {'DEGRADED', 'FAULTED', 'OFFLINE', 'UNAVAIL', 'REMOVED'}

_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'ok_card', 'degraded_card'],
    ['events'],
]


class ZFSHealthCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)

    def commands(self) -> List[Command]:
        return [Command("zpool list -H -o name,health 2>&1")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr

        if ret != 0 and not stdout.strip():
            return CollectResult.failed(f"zpool list failed: {stderr}")

        ok, degraded = 0, 0
        logs = []
        for line in stdout.splitlines():
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            pool, health = parts
            if health in _UNHEALTHY:
                degraded += 1
                logs.append((f"Pool {pool}: {health}", "ERROR"))
            else:
                ok += 1
                logs.append((f"Pool {pool}: {health}", "INFO"))

        total = ok + degraded
        if total == 0:
            return CollectResult(logs=[("No ZFS pools found", "WARNING")], status='offline')

        return CollectResult(
            metrics={"pools_total": total, "pools_ok": ok, "pools_degraded": degraded},
            logs=logs,
            status='failed' if degraded > 0 else 'online',
        )


class ZFSHealthUIPlugin(UIPlugin):
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
