from abc import ABC, abstractmethod

class BaseController(ABC):
    def __init__(self, name, target):
        self.name = name
        self.target = target

    @abstractmethod
    async def execute_action(self, action_name: str, **kwargs):
        """Execute a control action on the target system."""
        pass