from abc import ABC, abstractmethod
from typing import Any, Dict, List

class BasePlugin(ABC):
    """
    Standardized base class for all Vigil plugins.
    Encapsulates collection, alerting, presentation, and control logic for a specific domain.
    """
    def __init__(self, name: str, config: Dict[str, Any], internal_modules: Dict[str, Any]):
        self.name = name
        self.config = config
        self.target = config.get('target_host', 'localhost')
        self.interval = config.get('interval', 60)
        self.internal_modules = internal_modules

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