from abc import ABC, abstractmethod

class BaseCollector(ABC):
    def __init__(self, name, target, interval):
        self.name = name
        self.target = target
        self.interval = interval

    @abstractmethod
    async def collect(self):
        """Method to be implemented by specific collectors (Ping, SSH, etc.)"""
        pass