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
import asyncio
from typing import Any, Callable, Dict, List

from nicegui import binding, context, helpers
from nicegui import Client

from .components import safe_timer, offload

# Per-client registry of _PageScheduler instances, keyed by the NiceGUI
# Client's id. A group page with N expanded children used to mean N
# PluginPage instances each starting their own independent safe_timer — N
# separate offloaded DB round-trips every ~1s for one browser tab, and the
# more children a group has open the worse it gets. Every PluginPage built
# during the same client's render now shares ONE timer instead (see
# _PageScheduler), so a group's cost stops scaling with its child count.
_schedulers: Dict[str, "_PageScheduler"] = {}


class _PageScheduler:
    """
    Drives every PluginPage on one NiceGUI client from a single safe_timer.

    Plugins call self.page() → PluginPage.start() independently and have no
    idea whether they're standalone or one of a group's many expanded
    children — that's the point: this coalesces them transparently. Ticks
    at the fastest interval any registered PluginPage asked for, since a
    single shared timer can't run multiple periods at once and the pages
    already tolerate the write-batch window's staleness regardless of the
    interval they requested.
    """
    def __init__(self, client_id: str, interval: float):
        self._client_id = client_id
        self._interval = interval
        self._pages: List["PluginPage"] = []
        self._timer = None

    def add(self, page: "PluginPage") -> None:
        self._pages.append(page)
        if self._timer is None:
            self._timer = safe_timer(self._interval, self._tick, defer_first=True)
        elif self._interval > page._interval:
            # A later page asked for a shorter period than the scheduler
            # currently runs at (rare: pages normally share the default).
            # Cancel and restart at the faster rate so it isn't starved.
            self._interval = page._interval
            self._timer.cancel()
            self._timer = safe_timer(self._interval, self._tick, defer_first=True)

    async def _tick(self) -> None:
        # Dead pages (their element detached mid-group-collapse) are pruned
        # here rather than removed eagerly — cheap enough at this scale, and
        # avoids each PluginPage needing its own teardown hook into a
        # scheduler it doesn't otherwise know about.
        live = [p for p in self._pages if not p._detached()]
        self._pages = live
        if not live:
            if self._timer is not None:
                self._timer.cancel()
            _schedulers.pop(self._client_id, None)
            return
        await asyncio.gather(*(p._tick() for p in live))


def _scheduler_for_current_client(interval: float) -> _PageScheduler:
    client = context.client
    sched = _schedulers.get(client.id)
    if sched is None:
        sched = _PageScheduler(client.id, interval)
        _schedulers[client.id] = sched
    return sched


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

    One instance per render_ui() call (i.e. per page load / navigation, or
    per expanded child inside a group — see GroupUIPlugin). Doesn't own a
    timer itself: start() registers with the current client's _PageScheduler,
    which drives every PluginPage on that client from one shared safe_timer.
    That's what keeps a group's cost from scaling with how many children are
    expanded. Torn down via _detached() — the scheduler prunes a page once
    its own widgets are gone, the same detached-element signal safe_timer
    uses, so a closed browser tab or same-session navigation drops it
    without explicit cleanup here.
    """
    def __init__(self, plugin: Any, metric_names: List[str] = (),
                interval: float = 1.0):
        self.plugin = plugin
        self.model = PluginModel()
        self._metric_names = list(metric_names)
        self._refresh_callbacks: List[Callable[[], None]] = []
        self._interval = interval
        self._client = None

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
        Register with the current client's shared _PageScheduler. Call once
        render_ui() has finished building widgets and registering
        on_refresh() callbacks.

        Does one synchronous refresh immediately, before registering —
        without it, every widget shows its constructed default ('--', an
        empty table) until the first tick fires, which for a plain page
        load/HTTP response is indistinguishable from the page being broken:
        verified empirically that a single request's response is fully
        serialized before any deferred timer for that request can ever
        run, so "defer to the next tick" previously meant "never, for that
        response". The scheduler's own timer still uses defer_first=True —
        this first refresh already did the initial paint's DB read, so its
        immediate-first-call would just repeat it.

        Row-based widgets (metric_table, log_table, event_table,
        history_chart) each already do their own synchronous initial paint
        inline when built, before registering with on_refresh — so this only
        needs to cover the model's own scalar fields, not call the
        (possibly-async, offloaded) on_refresh callbacks a second time here.
        """
        self._refresh_model()
        self._client = context.client
        _scheduler_for_current_client(self._interval).add(self)

    def _detached(self) -> bool:
        return self._client is None or self._client.id not in Client.instances

    async def _tick(self) -> None:
        """
        One page's share of the scheduler's shared tick: its own DB reads
        (_refresh_model and each on_refresh callback), run off the event
        loop via `offload` — see that function's docstring for why a
        blocking read inline on the loop causes the dashboard's
        disconnects, not just its lag.
        """
        await self._refresh_model_async()
        for cb in self._refresh_callbacks:
            result = cb()
            if helpers.should_await(result):
                await result

    def _refresh_model(self) -> None:
        if self._metric_names:
            metrics = dict(self.model.metrics)
            for name in self._metric_names:
                m = self.plugin.latest_metric(name)
                metrics[name] = m.value if m is not None else None
            self.model.metrics = metrics

    async def _refresh_model_async(self) -> None:
        if not self._metric_names:
            return
        names = list(self._metric_names)
        values = await offload(
            lambda: [self.plugin.latest_metric(n) for n in names]
        )()
        metrics = dict(self.model.metrics)
        for name, m in zip(names, values):
            metrics[name] = m.value if m is not None else None
        self.model.metrics = metrics

    async def refresh_status(self) -> None:
        """Populate model.status/status_color from the plugin's latest
        StatusHistory row. Call (await) from an on_refresh() callback for
        plugins that display live status text/color. Reads through
        DatabaseManager.latest_status_cached — see that method's docstring
        — and runs off the event loop via offload() like every other
        widget refresh, since it's still a SQLite round-trip."""
        from .theme import STATUS_COLORS
        state = await offload(self.plugin.db.latest_status_cached)(self.plugin.id)
        self.model.status = state
        self.model.status_color = STATUS_COLORS.get(state, STATUS_COLORS['offline'])
