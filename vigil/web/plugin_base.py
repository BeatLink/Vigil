from abc import ABC, abstractmethod
from functools import partial
from typing import Any, Dict, List

from vigil.core.common.plugin_config import PluginConfigMixin
from vigil.web.ui.components import (render_host_card, render_status_card,
                                     metric_table, log_table, event_table)
from vigil.web.remote_proxy import RemoteSSHController, RemoteJobController


class UIPlugin(PluginConfigMixin, ABC):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        self._init_config(name, config)
        self.db = db
        self._collector_client = collector_client

        self.ssh_controller = RemoteSSHController(collector_client, self.id)
        self.job_controller = RemoteJobController(collector_client, self.id, db)

        self.internal_modules = {
            'ui': {
                'host_card': partial(render_host_card, self.target),
                'metrics_table': partial(metric_table, collector=self.id),
                'logs_table': partial(log_table, target=self.target, filter_prefix=self.id),
                'events_table': partial(event_table, plugin_name=self.name, plugin_id=self.id, target=self.target),
                'status_card': partial(render_status_card, collector=self.id),
            }
        }

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return await self._collector_client.action(self.id, action_id, kwargs)

    async def present(self) -> Dict[str, Any]:
        actions = await self._collector_client.actions(self.id)
        return {
            "name": self.name,
            "target": self.target,
            "actions": actions,
        }

    async def run_cycle(self) -> bool:
        return await self._collector_client.poll(self.id)

    def latest_metric(self, metric_name: str):
        return self.db.latest_metric_cached(self.id, metric_name)

    def page(self, metric_names: List[str] = (), interval: float = 1.0) -> "PluginPage":
        from vigil.web.ui.model import PluginPage
        return PluginPage(self, metric_names=metric_names, interval=interval)

    def latest_snapshot(self, default: Any = None) -> Any:
        import json
        raw = self.db.get_snapshot(self.id)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default

    @abstractmethod
    def render_ui(self, context: str = 'page'):
        pass
