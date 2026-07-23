"""
Config parsing shared by CollectorPlugin (vigil.collector.plugin_base) and
UIPlugin (vigil.web.plugin_base).

Split into its own module, rather than living alongside either base class,
because it is the one piece of plugin machinery genuinely needed by both
processes — everything else in a plugin is collector-only or web-only, and
importing this module pulls in nothing but stdlib plus parse_duration, so
neither process pays for the other's dependencies (real SSH machinery vs.
NiceGUI) just to compute a monitor's id/target/interval.
"""
from typing import Any, Dict, List

from vigil.core.common.time_utils import parse_duration


class PluginConfigMixin:
    """
    Config fields both CollectorPlugin and UIPlugin need, computed the same
    way in both processes so a monitor's `id`/`target`/`interval` never
    disagrees between them.
    """
    def _init_config(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.id = config.get('id', name)  # Unique identifier for the tree
        self.config = config
        self.interval = parse_duration(config.get('interval', 60))
        self.children: List[Any] = []
        # CollectorPlugin overwrites this with the real SSHConnection's host
        # once it constructs one (may differ if the connection defaults
        # port/host in ways this plain read of config does not); computed
        # here too so UIPlugin — which never constructs an SSHConnection —
        # still resolves the same target for id-based DB scoping.
        ssh_cfg = config.get('ssh_config', {})
        self.target = ssh_cfg.get('host', config.get('target_host', 'localhost'))
