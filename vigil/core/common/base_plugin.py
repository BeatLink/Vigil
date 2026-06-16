from abc import ABC, abstractmethod
from typing import Any, Dict, List
from vigil.core.common.ssh_connector import SSHConnection
from vigil.core.modules.collectors.ssh_collector import SSHCollector
from vigil.core.modules.controllers.ssh_controller import SSHController

class BasePlugin(ABC):
    """
    Standardized base class for all Vigil plugins.
    Encapsulates collection, alerting, presentation, and control logic for a specific domain.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        self.name = name
        self.config = config
        self.interval = config.get('interval', 60)

        # Initialize SSH infrastructure via the common library
        # The settings are passed down to allow the library to handle its own setup
        self.ssh_conn = SSHConnection.from_config(config)
        self.target = getattr(self.ssh_conn, 'host', config.get('target_host', 'localhost'))

        # Build the internal modules registry for use by subclasses
        self.internal_modules = {
            'collectors': {'ssh': SSHCollector(self.ssh_conn)},
            'controllers': {'ssh': SSHController(self.ssh_conn)},
            'loggers': {
                'db_logs': db.get_logger(self.target, self.name),
                'db_metrics': db.get_logger(self.target, self.name)
            }
        }

    @abstractmethod
    async def on_collect(self):
        """Triggered during the polling cycle to gather and log data."""
        pass

    def get_actions(self) -> List[Dict[str, str]]:
        """Returns a list of available control actions for this plugin."""
        return []

    @abstractmethod
    async def on_action(self, action_id: str, **kwargs) -> bool:
        """Executes a specific control action logic."""
        pass

    def present(self) -> Dict[str, Any]:
        """Formats data for the UI/Dashboard."""
        return {
            "name": self.name,
            "target": self.target,
            "actions": self.get_actions()
        }

    async def run_cycle(self):
        """Main execution entry point for the plugin's polling interval."""
        await self.on_collect()