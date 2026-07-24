import json
from functools import partial
from typing import Any, List, Optional


class WebStorageOrchestrator:
    def __init__(self, db: Any, plugin_id: str):
        self._db = db
        self._plugin_id = plugin_id

    def latest_metric(self, metric_name: str):
        return self._db.latest_metric_cached(self._plugin_id, metric_name)

    def latest_snapshot(self, default: Any = None) -> Any:
        raw = self._db.get_snapshot(self._plugin_id)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._db.get_setting(key, default)


class UIOrchestrator:
    def __init__(self, plugin: Any):
        from vigil.core.ui.ui.components import (render_host_card, render_status_card,
                                             metric_table, log_table, event_table,
                                             open_dialog_impl)

        self._plugin = plugin
        self.host_card = partial(render_host_card, plugin.target)
        self.metrics_table = partial(metric_table, collector=plugin.id)
        self.logs_table = partial(log_table, target=plugin.target, filter_prefix=plugin.id)
        self.events_table = partial(event_table, plugin_name=plugin.name, plugin_id=plugin.id, target=plugin.target)
        self.status_card = partial(render_status_card, collector=plugin.id)
        self.open_dialog = partial(open_dialog_impl, plugin)

    def page(self, metric_names: List[str] = (), interval: float = 1.0) -> "PluginPage":
        from vigil.core.ui.ui.model import PluginPage
        return PluginPage(self._plugin, metric_names=metric_names, interval=interval)
