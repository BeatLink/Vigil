from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple, Union

from vigil.core.common.plugin_helpers import PluginConfigMixin
from vigil.web.orchestration import WebStorageOrchestrator, UIOrchestrator
from vigil.web.remote_proxy import RemoteNetworkOrchestrator


class UIPlugin(PluginConfigMixin, ABC):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        self._init_config(name, config)
        self.db = db
        self._collector_client = collector_client

        self.network = RemoteNetworkOrchestrator(collector_client, self.id, db)
        self.storage = WebStorageOrchestrator(db, self.id)
        self.ui = UIOrchestrator(self)

    def plan_action(self, action_id: str, **kwargs) -> Optional[Union[Any, Any]]:
        return None

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return await self._collector_client.action(self.id, action_id, kwargs)

    async def action_with_output(self, action_id: str, **kwargs) -> Tuple[bool, str]:
        return await self._collector_client.action_with_output(self.id, action_id, kwargs)

    async def present(self) -> Dict[str, Any]:
        actions = await self._collector_client.actions(self.id)
        return {
            "name": self.name,
            "target": self.target,
            "actions": actions,
        }

    async def run_cycle(self) -> bool:
        return await self._collector_client.poll(self.id)

    @abstractmethod
    def render_ui(self, context: str = 'page'):
        pass
