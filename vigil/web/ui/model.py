"""
Bindable per-page data model for the web dashboard.

Replaces the polling half of on_data_event (components.py) — the path
DataBus.polling_mode falls back to, since the web process has no writer
thread of its own to ever call bus.emit() (see events.py). That path used to
mean N independent safe_timers per plugin page, one per widget, each doing
its own full DB round-trip and its own `.text =` / `.rows =` + `.update()`
every tick regardless of whether anything actually changed.

Here, PluginPage.model is refreshed by ONE shared timer per page. Two kinds
of widget consume it differently, because NiceGUI's binding only pushes to
the browser automatically for some properties — verified against NiceGUI's
own source before this was built:

  - Scalar fields (label text, a color) — ui.label().text is itself a
    BindableProperty whose on_change hook calls self.update(), so
    `label.bind_text_from(model, 'status')` is real, zero-code, automatic
    binding: NiceGUI's own loop (binding_refresh_interval) pushes changes
    to the browser with no plugin code involved. Per-metric values live in
    `model.metrics` (a plain dict) and bind the same way using NiceGUI's
    nested-key path support — `label.bind_text_from(model, ('metrics', 'vms_total'))`
    — verified empirically to resolve dict keys via Mapping.__getitem__.

  - Row-based widgets (ui.table.rows, ui.echart.options) — both are plain
    properties with no such hook; binding to them updates server-side
    Python state but never reaches the browser (verified against NiceGUI's
    Table/EChart source — neither wires `rows`/`options` as a
    BindableProperty). These stay on an explicit refresh step, registered
    with PluginPage.on_refresh() so it still runs off the ONE shared timer
    rather than each widget getting its own.

Net effect versus the old on_data_event: one DB round-trip per page per
tick (not one per widget), scalar fields get real diffing (no redundant
websocket writes for unchanged values), and row-based widgets still
refresh from a single shared driver instead of N independent timers.
"""
from typing import Any, Callable, Dict, List

from nicegui import binding

from .components import safe_timer


@binding.bindable_dataclass
class PluginModel:
    """Bind widgets to fields here — see this module's docstring for which
    widget properties that actually reaches the browser automatically."""
    status: str = 'offline'
    status_color: str = ''
    metrics: Dict[str, Any] = None

    def __post_init__(self):
        # Mutable dataclass defaults need a factory, not a shared literal;
        # bindable_dataclass forwards to dataclasses.dataclass, so the usual
        # __post_init__ pattern applies here.
        if self.metrics is None:
            self.metrics = {}


class PluginPage:
    """
    Owns one PluginModel and the single timer that refreshes it for a
    plugin's render_ui() call. Widgets bind to `.model`'s scalar fields
    directly (including per-metric values via `('metrics', name)` paths —
    see the module docstring); row-based widgets (tables, charts) register a
    refresh callback via `.on_refresh()` instead, so everything still rides
    the same timer tick without each widget needing its own.

    One instance per render_ui() call (i.e. per page load / navigation).
    Torn down the same way on_data_event subscriptions were — via
    safe_timer's own detached-element handling, so a closed browser tab or
    same-session navigation stops the timer without explicit cleanup here.
    """
    def __init__(self, plugin: Any, metric_names: List[str] = (),
                interval: float = 1.0):
        self.plugin = plugin
        self.model = PluginModel()
        self._metric_names = list(metric_names)
        self._refresh_callbacks: List[Callable[[], None]] = []
        self._timer = None
        self._interval = interval

    def on_refresh(self, callback: Callable[[], None]) -> None:
        """Register a callback (e.g. a table's own row-refresh) to run on
        every shared tick, alongside the model refresh."""
        self._refresh_callbacks.append(callback)

    def track_metric(self, name: str) -> None:
        """
        Add a metric name to refresh into `model.metrics` every tick, after
        construction — e.g. from render_status_card(), whose metric_name is
        only known once render_ui() builds that widget, later than the
        page(metric_names=[...]) call that typically starts this list.
        Safe to call multiple times with the same name.
        """
        if name not in self._metric_names:
            self._metric_names.append(name)

    def start(self) -> None:
        """
        Begin the shared refresh timer. Call once render_ui() has finished
        building widgets and registering on_refresh() callbacks.
        """
        def _tick():
            self._refresh_model()
            for cb in self._refresh_callbacks:
                cb()

        # defer_first: the widgets bound to `.model` already show its
        # constructed defaults on first paint; deferring means the timer's
        # actual DB reads happen on the next tick rather than racing
        # render_ui()'s own remaining setup code.
        self._timer = safe_timer(self._interval, _tick, defer_first=True)

    def _refresh_model(self) -> None:
        if self._metric_names:
            metrics = dict(self.model.metrics)
            for name in self._metric_names:
                m = self.plugin.latest_metric(name)
                metrics[name] = m.value if m is not None else None
            self.model.metrics = metrics

    def refresh_status(self) -> None:
        """Populate model.status/status_color from the plugin's latest
        StatusHistory row. Call from on_refresh() or _refresh_model()
        overrides for plugins that display live status text/color."""
        from vigil.core.data.database import StatusHistory
        from .theme import STATUS_COLORS
        row = (StatusHistory.select()
               .where(StatusHistory.collector_id == self.plugin.id)
               .order_by(StatusHistory.timestamp.desc())
               .first())
        state = row.state if row else 'offline'
        self.model.status = state
        self.model.status_color = STATUS_COLORS.get(state, STATUS_COLORS['offline'])
