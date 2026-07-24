from typing import Any, Dict, List

from vigil.core.common.time_utils import parse_duration


class PluginConfigMixin:
    def _init_config(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.id = config.get('id', name)
        self.config = config
        self.interval = parse_duration(config.get('interval', 60))
        self.children: List[Any] = []
        ssh_cfg = config.get('ssh_config', {})
        self.target = ssh_cfg.get('host', config.get('target_host', 'localhost'))
