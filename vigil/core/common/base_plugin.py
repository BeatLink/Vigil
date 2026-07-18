from abc import ABC, abstractmethod
from typing import Any, Dict, List
from vigil.core.common.ssh_connector import SSHConnection
from vigil.core.common.time_utils import parse_duration
from functools import partial
from vigil.core.ui.components import render_host_card, render_status_card, metric_table, log_table
from vigil.core.modules.collectors.ssh_collector import SSHCollector
from vigil.core.modules.controllers.ssh_controller import SSHController
from vigil.core.modules.controllers.job_controller import JobController

class BasePlugin(ABC):
    """
    Standardized base class for all Vigil plugins.
    Encapsulates collection, alerting, presentation, and control logic for a specific domain.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        self.name = name
        self.id = config.get('id', name)  # Unique identifier for the tree
        self.config = config
        self.interval = parse_duration(config.get('interval', 60))
        self.children: List['BasePlugin'] = []
        self.db = db

        # Initialize SSH infrastructure via the common library
        # The settings are passed down to allow the library to handle its own setup
        self.ssh_conn = SSHConnection.from_config(config)
        self.target = getattr(self.ssh_conn, 'host', config.get('target_host', 'localhost'))

        # Build the internal modules registry for use by subclasses
        self.internal_modules = {
            'collectors': {'ssh': SSHCollector(self.ssh_conn)},
            'controllers': {
                'ssh': SSHController(self.ssh_conn),
                # Long-running, cancellable, DB-tracked commands. Distinct from
                # 'ssh', which is capped at 30s and returns only a boolean.
                'job': JobController(self.ssh_conn, db, self.id, self.target),
            },
            'loggers': {
                'db_logs': db.get_logger(self.target, self.name),
                'db_metrics': db.get_logger(self.target, self.name)
            },
            'ui': {
                'host_card': partial(render_host_card, self.target),
                'metrics_table': partial(metric_table, self.name),
                'logs_table': partial(log_table, self.target, filter_prefix=self.name),
                'status_card': partial(render_status_card, self.name)
            }
        }

        # Convenience aliases — available in every plugin without repetitive __init__ boilerplate
        self.ssh_collector  = self.internal_modules['collectors'].get('ssh')
        self.ssh_controller = self.internal_modules['controllers'].get('ssh')
        self.job_controller = self.internal_modules['controllers'].get('job')
        self.db_logger      = self.internal_modules['loggers'].get('db_logs')
        self.db_metrics     = self.internal_modules['loggers'].get('db_metrics')

    def set_status(self, state: str):
        """Sets the current state of the plugin (online, warning, failed, offline)."""
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

    def latest_metric(self, metric_name: str):
        """Return the most recent Metric row for this plugin, or None."""
        from vigil.core.data.database import Metric
        return (
            Metric.select()
            .where((Metric.collector == self.name) & (Metric.metric_name == metric_name))
            .order_by(Metric.timestamp.desc())
            .first()
        )

    @abstractmethod
    def render_ui(self, context: str = 'page'):
        """Render the plugin UI.

        context:
          'page'   — standalone full-page view (all widgets visible).
          'inline' — embedded inside a group panel (host_card and logs hidden).
        """
        pass