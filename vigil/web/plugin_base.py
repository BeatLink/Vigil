"""
UIPlugin: the web-process half of Vigil's plugin split.

See vigil.collector.plugin_base for the full rationale (CollectorPlugin vs.
UIPlugin as separate classes rather than one class with a mode flag). This
class holds no SSH connection and does no collection — `on_collect` does not
exist here at all, so a plugin author cannot accidentally call it from
render_ui(). `on_action`, `ssh_controller`, and `job_controller` are thin
proxies to the collector's internal API (see remote_proxy.py), so the
handful of plugins that call those directly from render_ui() (processes.py's
Kill button, service_list.py's unit-file viewer, borg.py's job control) keep
working unmodified — the network hop is hidden behind the same method names
CollectorPlugin exposes. `latest_metric` and the `internal_modules['ui']`
widgets are plain DB reads, identical to the collector-side versions, since
SQLite's WAL mode serves concurrent readers regardless of which process is
writing.
"""
from abc import ABC, abstractmethod
from functools import partial
from typing import Any, Dict, List

from vigil.core.common.plugin_config import PluginConfigMixin
from vigil.web.ui.components import (render_host_card, render_status_card,
                                     metric_table, log_table, event_table)
from vigil.web.remote_proxy import RemoteSSHController, RemoteJobController


class UIPlugin(PluginConfigMixin, ABC):
    """Web-process plugin: renders a monitor's dashboard page."""

    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        self._init_config(name, config)
        self.db = db
        # HTTP client for the collector's internal API — see
        # vigil.web.remote_proxy.CollectorClient.
        self._collector_client = collector_client

        self.ssh_controller = RemoteSSHController(collector_client, self.id)
        self.job_controller = RemoteJobController(collector_client, self.id, db)

        # host_card takes no `page` — it's a static label, not something
        # that refreshes. The other four all take `page` as their first
        # argument at call time (`internal_modules['ui']['logs_table'](page)`)
        # rather than being pre-bound partials: each needs the specific
        # PluginPage instance render_ui() built for the current call, which
        # doesn't exist yet at __init__ time (see UIPlugin.page()).
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
        """
        Proxy an action to the collector over its internal API.

        Kept on the base class (rather than requiring every UIPlugin subclass
        to implement it) because, unlike CollectorPlugin.on_action, there is
        never a UI-side implementation to write — the real logic always lives
        collector-side; this is purely a network hop.
        """
        return await self._collector_client.action(self.id, action_id, kwargs)

    async def present(self) -> Dict[str, Any]:
        """
        Formats data for the UI/Dashboard.

        Unlike CollectorPlugin.present() (sync — get_actions() there is a
        plain in-memory list built from config), this is async: the
        available actions live on the collector-side plugin instance
        (get_actions() can depend on live config the UI process never
        constructs, e.g. vms.py's expect_running), so they're fetched over
        the internal API rather than duplicated as a second get_actions()
        implementation here.
        """
        actions = await self._collector_client.actions(self.id)
        return {
            "name": self.name,
            "target": self.target,
            "actions": actions,
        }

    async def run_cycle(self) -> bool:
        """Ask the collector to poll this monitor now ("Poll Now" button)."""
        return await self._collector_client.poll(self.id)

    def latest_metric(self, metric_name: str):
        """Same query as CollectorPlugin.latest_metric — a plain DB read."""
        from vigil.core.data.database import Metric
        return (
            Metric.select()
            .where((Metric.collector == self.id) & (Metric.metric_name == metric_name))
            .order_by(Metric.timestamp.desc())
            .first()
        )

    def page(self, metric_names: List[str] = (), interval: float = 1.0) -> "PluginPage":
        """
        Build this plugin's PluginPage for the current render_ui() call —
        one bindable model + one shared refresh timer for the whole page,
        replacing what used to be a separate on_data_event timer per widget.
        See vigil.web.ui.model for the binding-vs-explicit-refresh split and
        why it exists.

        `metric_names` are fetched into `model.metrics` every tick — pass
        the metric names this page's own widgets bind to directly (via
        `label.bind_text_from(page.model, ('metrics', name))`); shared
        widgets built through `internal_modules['ui']` (logs_table,
        events_table, status_card) register their own refresh via
        `page.on_refresh(...)` and need no entry here.

        Call `page.start()` once render_ui() has finished building widgets.
        """
        from vigil.web.ui.model import PluginPage
        return PluginPage(self, metric_names=metric_names, interval=interval)

    def latest_snapshot(self, default: Any = None) -> Any:
        """
        Return this monitor's latest row-level data snapshot (see
        PluginSnapshot in core/data/database.py), decoded from JSON.

        For plugins whose UI needs more than scalar metrics — a process
        list, a systemd unit list — where the collector's on_collect()
        writes rows via db_logger.snapshot(rows) and render_ui() here reads
        them back with this method. Returns `default` (a `[]` or `{}` the
        caller can render directly, typically) if the collector has never
        written a snapshot yet — e.g. before its first poll.
        """
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
        """Render the plugin UI.

        context:
          'page'   — standalone full-page view (all widgets visible).
          'inline' — embedded inside a group panel (host_card and logs hidden).
        """
        pass
