"""ZFS pool state and capacity, via zpool."""

from typing import Any, Dict, List

from vigil.plugins.base.signal_plugin import (
    LOG_LEVEL as _LOG_LEVEL, SignalPlugin, worst_status as _worst,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig
from vigil.plugins.base.plugin_helpers import level_for as _level_for


_UNHEALTHY = {'DEGRADED', 'FAULTED', 'OFFLINE', 'UNAVAIL', 'REMOVED'}


def _sanitize_pool(name: str) -> str:
    """A pool name reduced to the characters a metric name may carry."""
    return ''.join(c if c.isalnum() or c == '_' else '_' for c in name.lower())


class Zfs(SignalPlugin):
    """ZFS pool state and capacity from one `zpool list`, reporting a count of
    degraded pools plus a usage metric per pool and the fullest pool's usage."""

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.warning = float(config.get('warning', 80))
        self.threshold = float(config.get('threshold', 90))
        self.pools = list(config.get('pools') or [])

        from vigil.core.ui.spec import register_item_color_rule, register_color_rule, threshold_color
        self._color_rule = f'zfs_usage_{self.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))
        self._item_color_rule = f'zfs_pool_{self.id}'
        register_item_color_rule(self._item_color_rule)(
            lambda item: _level_for(item.get('value') or 0.0, self.warning, self.threshold))

    def commands(self) -> List[Command]:
        return [Command("zpool list -H -o name,health,capacity " + " ".join(self.pools) + " 2>&1")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0 and not stdout.strip():
            return CollectResult.failed(f"zpool list failed: {stderr or stdout}")

        ok, degraded = 0, 0
        usage: Dict[str, float] = {}
        logs, statuses = [], []
        for line in stdout.splitlines():
            parts = line.strip().split()
            if len(parts) != 3:
                continue
            pool, health, capacity = parts
            try:
                usage_pct = float(capacity.rstrip('%'))
            except ValueError:
                continue
            usage[pool] = usage_pct

            if health in _UNHEALTHY:
                degraded += 1
                status = 'failed'
            else:
                ok += 1
                status = _level_for(usage_pct, self.warning, self.threshold)
            statuses.append(status)
            logs.append((
                f"Pool {pool}: {health}, {usage_pct:.0f}% used "
                f"(warn {self.warning:g}% / fail {self.threshold:g}%)",
                _LOG_LEVEL[status],
            ))

        if not usage:
            return CollectResult(logs=[("No ZFS pools found", "WARNING")], status='offline')

        metrics = {
            'pools_total': ok + degraded,
            'pools_ok': ok,
            'pools_degraded': degraded,
            'zfs_usage_max': max(usage.values()),
        }
        for pool, usage_pct in usage.items():
            metrics[f'pool_usage_{_sanitize_pool(pool)}'] = usage_pct

        return CollectResult(metrics=metrics, logs=logs, status=_worst(statuses))

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'zfs_total_card': {'metric': 'pools_total', 'title': 'POOLS', 'format': 'int'},
            'zfs_ok_card': {
                'metric': 'pools_ok', 'title': 'HEALTHY', 'format': 'int',
                'color': 'always_online',
            },
            'zfs_degraded_card': {
                'metric': 'pools_degraded', 'title': 'DEGRADED', 'format': 'int',
                'color': 'nonzero_failed',
            },
            'zfs_usage_card': {
                'metric': 'zfs_usage_max', 'title': 'FULLEST POOL',
                'format': 'percent1', 'color': self._color_rule,
            },
            'zfs_pools': {
                'repeat': {
                    'source': 'metrics_prefix',
                    'metrics_prefix': 'pool_usage_', 'metrics_suffix': '',
                    'item_format': 'percent1',
                    'item_color_by': self._item_color_rule,
                    'label_transform': 'spaces_upper',
                    'container': 'cards',
                    'empty_text': 'No ZFS pools found',
                },
            },
        }

    def card_row(self) -> List[str]:
        return ['zfs_total_card', 'zfs_ok_card', 'zfs_degraded_card', 'zfs_usage_card']

    def rows(self) -> List[List[str]]:
        return [['zfs_pools']]

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'zfs_chart': {'metric': 'zfs_usage_max', 'title': 'FULLEST POOL CAPACITY (%)'}}
