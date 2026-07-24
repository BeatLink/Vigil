from typing import Dict, Any, List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult


_DEFAULT_LAYOUT = [
    ['host_card', 'pool_card', 'usage_card', 'threshold_card'],
    ['chart'],
    ['events'],
]


class ZFSPool(Plugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.pool = config.get('pool')
        self.threshold = int(config.get('threshold', 90))

        from vigil.core.ui.spec import register_color_rule
        self._color_rule_name = f'zfs_pool_threshold_{self.id}'

        @register_color_rule(self._color_rule_name)
        def _usage_color(v, _threshold=self.threshold):
            if v is None:
                return None
            return 'failed' if v >= _threshold else 'online'

    def commands(self) -> List[Command]:
        return [Command(f"zpool list -H -o name,capacity {self.pool}")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr

        if ret != 0:
            return CollectResult.failed(f"zpool list failed: {stderr}")

        try:
            usage_pct = float(stdout.strip().split()[1].rstrip('%'))
        except (IndexError, ValueError) as e:
            return CollectResult.failed(f"Failed to parse zpool output '{stdout.strip()}': {e}")

        level = "WARNING" if usage_pct >= self.threshold else "INFO"
        return CollectResult(
            metrics={"usage_pct": usage_pct},
            logs=[(f"Pool {self.pool}: {usage_pct:.1f}% used (threshold {self.threshold}%)", level)],
            status='failed' if usage_pct >= self.threshold else 'online',
        )

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
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)
