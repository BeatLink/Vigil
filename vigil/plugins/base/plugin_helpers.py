from typing import Any, Dict, List

from vigil.plugins.base.time_utils import parse_duration


class PluginConfigMixin:
    def _init_config(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.id = config.get('id', name)
        self.config = config
        self.interval = parse_duration(config.get('interval', 60))
        self.children: List[Any] = []
        ssh_cfg = config.get('ssh_config', {})
        self.target = ssh_cfg.get('host', config.get('target_host', 'localhost'))


def level_for(value: float, warning: float, threshold: float) -> str:
    if value >= threshold:
        return 'failed'
    if value >= warning:
        return 'warning'
    return 'online'


def format_bytes(gb: float) -> str:
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{gb * 1024:.0f} MB"
