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
        self.id = config.get('id', name)  # Unique identifier for the tree
        self.config = config
        self.interval = config.get('interval', 60)
        self.children: List['BasePlugin'] = []
        self.db = db

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

    def set_status(self, state: str):
        """Sets the current state of the plugin (success, warning, fail, inactive)."""
        self.db.insert_status(self.id, state)

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

    def render_ui(self):
        """Default UI implementation showing metrics and events. Override this in subclasses."""
        from nicegui import ui
        from vigil.core.data.database import Metric, Event

        with ui.grid(columns=2).classes('w-full gap-4'):
            # Monitor Metrics
            with ui.card().classes('p-4 shadow-sm'):
                ui.label('Monitor Metrics').classes('font-bold mb-2 text-primary')
                p_metric_table = ui.table(columns=[
                    {'name': 'ts', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
                    {'name': 'name', 'label': 'Metric', 'field': 'metric_name', 'align': 'left'},
                    {'name': 'val', 'label': 'Value', 'field': 'value', 'align': 'left'},
                ], rows=[]).classes('w-full border-none')
                
                def update_pm():
                    query = Metric.select().where(Metric.collector == self.name).order_by(Metric.timestamp.desc()).limit(15)
                    p_metric_table.rows[:] = [m.__data__ for m in query]
                ui.timer(5.0, update_pm)

            # Monitor Logs/Events
            with ui.card().classes('p-4 shadow-sm'):
                ui.label('Recent Logs').classes('font-bold mb-2 text-primary')
                p_event_table = ui.table(columns=[
                    {'name': 'ts', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
                    {'name': 'lvl', 'label': 'Level', 'field': 'level', 'align': 'left'},
                    {'name': 'msg', 'label': 'Message', 'field': 'message', 'align': 'left'},
                ], rows=[]).classes('w-full border-none')

                def update_pe():
                    query = Event.select().where(
                        (Event.target == self.target) & (Event.message.contains(f"[{self.name}]"))
                    ).order_by(Event.timestamp.desc()).limit(15)
                    p_event_table.rows[:] = [e.__data__ for e in query]
                ui.timer(5.0, update_pe)