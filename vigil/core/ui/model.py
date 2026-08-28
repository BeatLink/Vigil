"""Dashboard refresh model.

The dashboard refreshes on push, not on a poll. Every semantic write on the
Database Engine publishes a change (``core/state/changes.py``); each connected
browser client owns one ``_PageScheduler`` subscribed to that bus, and a change
wakes it to re-render, so a status flip reaches the screen as soon as it is
written rather than at the next tick of a timer. An idle system publishes
nothing, but is not free: the ``IDLE_REFRESH_SECONDS`` sweep still re-ticks
every tickable, because ages ("2m ago", a running job's duration) change with
no write to announce them — the diffing keeps those wakeups off the wire.

Waking is leading-edge debounced: the first change refreshes immediately, and
further changes arriving during the cooldown collapse into one trailing
refresh. That keeps latency at zero for an isolated event while bounding the
refresh rate on a busy system, where a single collection cycle publishes a
change for its metrics plus one per log line and status it writes.

A change wakes every tickable on the client rather than only those interested
in the changed monitor. Refresh callbacks are registered by plugin render code
and may read any monitor's data — a group page renders its children — so there
is no reliable interest set to filter on.

A tickable lives as long as the elements it redraws, not as long as the client.
Each anchors itself to a hidden marker in the slot it was registered in, and
counts as detached once that marker leaves ``client.elements``. Switching views
clears the main container, which deletes those markers and so retires the old
view's tickables; the sidebar's own callbacks sit outside that container and
survive. Scoping to the client instead would accumulate one dead tickable per
view visited, each still refreshing widgets the browser no longer has.
"""

import asyncio
from typing import Any, Dict, List

from nicegui import context, ui

from vigil.core.contracts import RefreshCallback
from vigil.core.ui import nicegui_compat as ng
from vigil.core.state import CHANGES
from .components import offload

_schedulers: Dict[str, "_PageScheduler"] = {}


def _current_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.get_event_loop()

# Minimum gap between two refreshes of the same client.
COOLDOWN_SECONDS = 0.5

# Backstop sweep: redraws values that age on their own (a running job's
# duration, "last seen" times) and reaps a client that closed while the system
# had nothing to publish.
IDLE_REFRESH_SECONDS = 5.0


def _anchor_element():
    """A hidden marker in the slot being rendered, whose removal is the signal
    that the widgets registered alongside it are gone."""
    try:
        return ui.element('span').style('display: none')
    except RuntimeError:
        # No slot to anchor to (headless/tests); fall back to client lifetime.
        return None


def _is_detached(client, anchor) -> bool:
    """True once the client disconnected or the anchor's container was cleared."""
    if not ng.client_is_live(client):
        return True
    return anchor is not None and not ng.element_is_attached(client, anchor)


class _PageScheduler:
    """One change subscription per browser client, waking every registered
    *tickable* — a PluginPage or a bare callback (via _CallbackTick). A
    tickable is anything with ``_tick()`` (async) and ``_detached()``.
    Coordinating every card, chart, table and overview refresh for a client
    onto a single subscription avoids the fan of independent per-widget timers
    the UI used to spin up."""

    def __init__(self, client_id: str):
        self._client_id = client_id
        self._tickables: List[Any] = []
        self._loop = _current_loop()
        self._pending = False
        self._runner: Any = None
        self._idle: Any = None
        self._unsubscribe = None

    def add(self, tickable: Any) -> None:
        self._tickables.append(tickable)
        if self._unsubscribe is None:
            self._unsubscribe = CHANGES.subscribe(self._on_change)
            self._idle = asyncio.create_task(self._idle_loop())
        asyncio.create_task(tickable._tick())

    def _on_change(self, kind: str, plugin_id) -> None:
        """Bus subscriber. Runs on whichever thread performed the write, so it
        only hands the wake-up to the UI's loop."""
        try:
            self._loop.call_soon_threadsafe(self._wake)
        except RuntimeError:
            # The loop is gone (shutdown); drop the subscription and stop.
            # Cancelling the tasks is the loop's job, not this thread's.
            self._unsubscribe, drop = None, self._unsubscribe
            if drop is not None:
                drop()

    def _wake(self) -> None:
        self._pending = True
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while self._pending:
            self._pending = False
            await self._tick()
            if self._unsubscribe is None:
                return
            await asyncio.sleep(COOLDOWN_SECONDS)

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(IDLE_REFRESH_SECONDS)
            await self._tick()
            if self._unsubscribe is None:
                return

    async def _tick(self) -> None:
        live = [t for t in self._tickables if not t._detached()]
        self._tickables = live
        if not live:
            self._shutdown()
            return
        await asyncio.gather(*(t._tick() for t in live))

    def _shutdown(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        idle, self._idle = self._idle, None
        if idle is not None:
            idle.cancel()
        # Only if a replacement has not already claimed the id.
        if _schedulers.get(self._client_id) is self:
            _schedulers.pop(self._client_id, None)


class _CallbackTick:
    """Adapts a bare refresh callback to the scheduler's tickable protocol so
    overview-page refreshes (on_data_event) share the client's single change
    subscription instead of each owning an independent timer. Detachment is
    tied to the slot the callback was registered in — same lifetime as a
    PluginPage."""

    def __init__(self, callback: RefreshCallback, run_now: bool):
        self._callback = callback
        self._client = context.client
        self._anchor = _anchor_element()
        self._ran_once = False
        self._run_now = run_now

    def _detached(self) -> bool:
        return _is_detached(self._client, self._anchor)

    async def _tick(self) -> None:
        # Honour run_now=False by skipping the very first (inline) tick.
        if not self._ran_once and not self._run_now:
            self._ran_once = True
            return
        self._ran_once = True
        result = self._callback()
        if ng.should_await(result):
            await result


def _scheduler_for_current_client() -> _PageScheduler:
    client = context.client
    sched = _schedulers.get(client.id)
    if sched is None:
        sched = _PageScheduler(client.id)
        _schedulers[client.id] = sched
    return sched


def schedule_callback(callback: RefreshCallback, run_now: bool = True) -> None:
    """Register a plain refresh callback on the current client's shared
    scheduler. The single entry point the overview page's on_data_event uses so
    its refreshes ride the same change subscription as everything else on the
    page."""
    _scheduler_for_current_client().add(_CallbackTick(callback, run_now))


@ng.bindable_dataclass
class PluginModel:
    status: str = 'offline'
    status_color: str = ''
    metrics: Dict[str, Any] = None

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}


class PluginPage:
    def __init__(self, plugin: Any, metric_names: List[str] = ()):
        self.plugin = plugin
        self.model = PluginModel()
        self._metric_names = list(metric_names)
        self._refresh_callbacks: List[RefreshCallback] = []
        self._client = None
        self._anchor = None

    def on_refresh(self, callback: RefreshCallback) -> None:
        self._refresh_callbacks.append(callback)

    def track_metric(self, name: str) -> None:
        if name not in self._metric_names:
            self._metric_names.append(name)

    def start(self) -> None:
        self._client = context.client
        self._anchor = _anchor_element()
        _scheduler_for_current_client().add(self)

    def _detached(self) -> bool:
        return _is_detached(self._client, self._anchor)

    async def _tick(self) -> None:
        await self._refresh_model_async()
        for cb in self._refresh_callbacks:
            result = cb()
            if ng.should_await(result):
                await result

    async def _refresh_model_async(self) -> None:
        if not self._metric_names:
            return
        names = list(self._metric_names)
        values = await offload(
            lambda: [self.plugin.data.latest_metric(n) for n in names]
        )()
        metrics = dict(self.model.metrics)
        for name, m in zip(names, values):
            metrics[name] = m.value if m is not None else None
        self.model.metrics = metrics
