from typing import Dict, Any, List
from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin

from vigil.core.common.plugin_utils import format_bytes as _format_gb


_DEFAULT_LAYOUT = [
    ['host_card', 'path_card', 'threshold_card'],
    ['usage_card', 'avail_card', 'total_card'],
    ['chart'],
    ['events'],
]


class DiskSpaceCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.path = config.get('path', '/')
        self.threshold = int(config.get('threshold', 90))

    def commands(self) -> List[Command]:
        return [Command(f"df --output=size,used,avail,pcent -B1 '{self.path}' | tail -1")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"df failed for '{self.path}': {stderr}")

        try:
            fields = stdout.strip().split()
            size_bytes = int(fields[0])
            used_bytes = int(fields[1])
            avail_bytes = int(fields[2])
            used_pct = float(fields[3].rstrip('%'))
        except (IndexError, ValueError) as e:
            return CollectResult.failed(f"Failed to parse df output '{stdout.strip()}': {e}")

        size_gb  = size_bytes  / (1024 ** 3)
        used_gb  = used_bytes  / (1024 ** 3)
        avail_gb = avail_bytes / (1024 ** 3)

        metrics = {
            'used_pct': used_pct,
            'size_gb': size_gb,
            'used_gb': used_gb,
            'avail_gb': avail_gb,
        }

        level = 'WARNING' if used_pct >= self.threshold else 'INFO'
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"{self.path}: {used_pct:.1f}% used "
                f"({_format_gb(used_gb)} of {_format_gb(size_gb)}, "
                f"{_format_gb(avail_gb)} free, threshold {self.threshold}%)",
                level,
            )],
            status='failed' if used_pct >= self.threshold else 'online',
        )


class DiskSpaceUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.path = config.get('path', '/')
        self.threshold = int(config.get('threshold', 90))

        from vigil.web.ui.spec import register_color_rule, threshold_color
        self._color_rule_name = f'disk_space_threshold_{self.id}'
        register_color_rule(self._color_rule_name)(
            threshold_color(warning=self.threshold, threshold=self.threshold))

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'path_card': {'title': 'PATH', 'value_attr': 'path'},
                'threshold_card': {'title': 'THRESHOLD', 'value_attr': 'threshold', 'value_format': '{}%'},
                'usage_card': {
                    'metric': 'used_pct', 'title': 'USAGE',
                    'format': 'percent1', 'color': self._color_rule_name,
                },
                'avail_card': {'metric': 'avail_gb', 'title': 'AVAILABLE', 'format': 'bytes_gb'},
                'total_card': {'metric': 'size_gb', 'title': 'TOTAL SIZE', 'format': 'bytes_gb'},
            },
            'chart': {'metric': 'used_pct', 'title': f'USAGE HISTORY — {self.path} (%)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
