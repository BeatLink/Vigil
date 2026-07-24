from typing import Dict, Any

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin


_DEFAULT_LAYOUT = [
    ['host_card', 'pool_card', 'usage_card', 'threshold_card'],
    ['chart'],
    ['events'],
]


class ZFSPoolCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.pool = config.get('pool')
        self.threshold = int(config.get('threshold', 90))

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            f"zpool list -H -o name,capacity {self.pool}"
        )

        if ret != 0:
            self.db_logger.write(f"zpool list failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        try:
            usage_pct = float(stdout.strip().split()[1].rstrip('%'))
        except (IndexError, ValueError) as e:
            self.db_logger.write(f"Failed to parse zpool output '{stdout.strip()}': {e}", level="ERROR")
            self.set_status('failed')
            return

        self.db_metrics.metric("usage_pct", usage_pct)
        level = "WARNING" if usage_pct >= self.threshold else "INFO"
        self.db_logger.write(
            f"Pool {self.pool}: {usage_pct:.1f}% used (threshold {self.threshold}%)",
            level=level
        )
        self.set_status('failed' if usage_pct >= self.threshold else 'online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class ZFSPoolUIPlugin(UIPlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool = self.config.get('pool')
        self.threshold = int(self.config.get('threshold', 90))

        from vigil.web.ui.spec import register_color_rule
        self._color_rule_name = f'zfs_pool_threshold_{self.id}'

        @register_color_rule(self._color_rule_name)
        def _usage_color(v, _threshold=self.threshold):
            if v is None:
                return None
            return 'failed' if v >= _threshold else 'online'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'pool_card': {'title': 'POOL', 'value_attr': 'pool'},
                'threshold_card': {'title': 'THRESHOLD', 'value_attr': 'threshold', 'value_format': '{}%'},
                'usage_card': {
                    'metric': 'usage_pct', 'title': 'USAGE',
                    'format': 'percent1', 'color': self._color_rule_name,
                },
            },
            'chart': {'metric': 'usage_pct', 'title': f'CAPACITY HISTORY — {self.pool} (%)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
