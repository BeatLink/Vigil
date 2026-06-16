from abc import ABC, abstractmethod
from typing import Any, Dict, List

class BasePlugin(ABC):
    """
    Standardized base class for all Vigil plugins.
    Encapsulates collection, alerting, presentation, and control logic for a specific domain.
    """
    def __init__(self, name: str, target: str, interval: int):
        self.name = name
        self.target = target
        self.interval = interval

    @abstractmethod
    async def collect(self) -> Any:
        """Gathers data from the target system."""
        pass

    @abstractmethod
    async def alert(self, data: Any):
        """Evaluates thresholds and triggers notifications based on collected data."""
        pass

    @abstractmethod
    async def control(self, action: str, **kwargs) -> bool:
        """Executes remediation or control actions on the target system."""
        pass

    def present(self, data: Any) -> Dict[str, Any]:
        """Formats data for the UI/Dashboard."""
        return {
            "name": self.name,
            "target": self.target,
            "data": data
        }

    async def run_cycle(self):
        """Main execution entry point for the plugin's polling interval."""
        data = await self.collect()
        await self.alert(data)