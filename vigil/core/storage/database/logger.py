import logging
from typing import Optional, Any
from vigil.core.database.manager import VigilDatabase

class InternalDatabaseLogger:
    """
    Abstraction layer for plugins to write logs and metrics 
    without touching the ORM or DB Manager directly.
    """
    def __init__(self, db_manager: VigilDatabase, target: str, plugin_name: str):
        self.db = db_manager
        self.target = target
        self.plugin_name = plugin_name

    def write(self, message: str, level: str = "INFO"):
        """Writes a log entry/event to the database."""
        self.db.insert_event(
            level=level,
            message=message,
            target=self.target
        )

    def record(self, metric_name: str, value: float, metadata: Optional[Any] = None):
        """Records a numerical metric to the database."""
        self.db.insert_metric(
            target=self.target,
            collector=self.plugin_name,
            metric_name=metric_name,
            value=value,
            metadata=str(metadata) if metadata else None
        )