import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from vigil.core.common.plugin_config import PluginConfigMixin
from vigil.core.common.ssh_connector import SSHConnection
from vigil.core.common.time_utils import parse_duration
from vigil.collector.collectors.ssh_collector import SSHCollector, TIMEOUT as SSH_TIMEOUT
from vigil.collector.controllers.ssh_controller import SSHController
from vigil.collector.controllers.job_controller import JobController


class CollectorPlugin(PluginConfigMixin, ABC):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        self._init_config(name, config)
        self.db = db
        self._collecting = False
        self._last_collected = 0.0

        self.ssh_conn = SSHConnection.from_config(config)
        self.target = getattr(self.ssh_conn, 'host', config.get('target_host', 'localhost'))

        self.timeout = parse_duration(config.get('timeout', SSH_TIMEOUT))

        self.internal_modules = {
            'collectors': {'ssh': SSHCollector(self.ssh_conn, timeout=self.timeout)},
            'controllers': {
                'ssh': SSHController(self.ssh_conn),
                'job': JobController(self.ssh_conn, db, self.id, self.target),
            },
            'loggers': {
                'db_logs': db.get_logger(self.target, self.name, self.id),
                'db_metrics': db.get_logger(self.target, self.name, self.id)
            },
        }

        self.ssh_collector  = self.internal_modules['collectors'].get('ssh')
        self.ssh_controller = self.internal_modules['controllers'].get('ssh')
        self.job_controller = self.internal_modules['controllers'].get('job')
        self.db_logger      = self.internal_modules['loggers'].get('db_logs')
        self.db_metrics     = self.internal_modules['loggers'].get('db_metrics')

    def set_status(self, state: str):
        self.db.insert_status(self.id, state)

    @abstractmethod
    async def on_collect(self):
        pass

    def get_actions(self) -> List[Dict[str, str]]:
        return []

    @abstractmethod
    async def on_action(self, action_id: str, **kwargs) -> bool:
        pass

    def present(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "actions": self.get_actions()
        }

    async def run_cycle(self) -> bool:
        if self._collecting:
            logging.debug(
                f"{self.name}: previous collection still running, skipping this tick"
            )
            return False

        now = time.monotonic()
        if self._last_collected and (now - self._last_collected) < self.interval:
            return False

        self._collecting = True
        try:
            await self.on_collect()
            return True
        finally:
            self._last_collected = time.monotonic()
            self._collecting = False

    def latest_metric(self, metric_name: str):
        from vigil.core.data.database import Metric
        return (
            Metric.select()
            .where((Metric.collector == self.id) & (Metric.metric_name == metric_name))
            .order_by(Metric.timestamp.desc())
            .first()
        )
