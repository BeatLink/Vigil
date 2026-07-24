from functools import partial
from typing import Any, List


class UIOrchestrator:
    def __init__(self, plugin: Any):
        from vigil.web.ui.components import (render_host_card, render_status_card,
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
        from vigil.web.ui.model import PluginPage
        return PluginPage(self._plugin, metric_names=metric_names, interval=interval)
