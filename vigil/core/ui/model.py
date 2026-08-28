"""Dashboard refresh model.

The dashboard refreshes on push, not on a poll. Every semantic write on the
Database Engine publishes a change (``core/state/changes.py``); each connected
browser client owns one ``_PageScheduler`` subscribed to that bus, and a change
wakes it to re-render. An idle system produces no changes and so costs nothing,
while a status flip reaches the screen as soon as it is written rather than at
the next tick of a timer.

Waking is leading-edge debounced: the first change refreshes immediately, and
further changes arriving during the cooldown collapse into one trailing
refresh. That keeps latency at zero for an isolated event while bounding the
refresh rate on a busy system, where a single collection cycle publishes a
change per metric, log line and status it writes.

A change wakes every tickable on the client rather than only those interested
in the changed monitor. Refresh callbacks are registered by plugin render code
and may read any monitor's data — a group page renders its children — so there
is no reliable interest set to filter on.
"""

import asyncio
from typing import Any, Dict, List

from nicegui import binding, context, helpers
from nicegui import Client

from vigil.core.contracts import RefreshCallback
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
        _schedulers.pop(self._client_id, None)


class _CallbackTick:
    """Adapts a bare refresh callback to the scheduler's tickable protocol so
    overview-page refreshes (on_data_event) share the client's single change
    subscription instead of each owning an independent timer. Detachment is
    tied to the client the callback was registered under — same lifetime as a
    PluginPage."""

    def __init__(self, callback: RefreshCallback, run_now: bool):
        self._callback = callback
        self._client = context.client
        self._ran_once = False
        self._run_now = run_now

    def _detached(self) -> bool:
        return self._client is None or self._client.id not in Client.instances

    async def _tick(self) -> None:
        # Honour run_now=False by skipping the very first (inline) tick.
        if not self._ran_once and not self._run_now:
            self._ran_once = True
            return
        self._ran_once = True
        result = self._callback()
        if helpers.should_await(result):
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


@binding.bindable_dataclass
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

    def on_refresh(self, callback: RefreshCallback) -> None:
        self._refresh_callbacks.append(callback)

    def track_metric(self, name: str) -> None:
        if name not in self._metric_names:
            self._metric_names.append(name)

    def start(self) -> None:
        self._client = context.client
        _scheduler_for_current_client().add(self)

    def _detached(self) -> bool:
        return self._client is None or self._client.id not in Client.instances

    async def _tick(self) -> None:
        await self._refresh_model_async()
        for cb in self._refresh_callbacks:
            result = cb()
            if helpers.should_await(result):
                await result

    def _refresh_model(self) -> None:
        if self._metric_names:
            metrics = dict(self.model.metrics)
            for name in self._metric_names:
                m = self.plugin.data.latest_metric(name)
                metrics[name] = m.value if m is not None else None
            self.model.metrics = metrics

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

    async def refresh_status(self) -> None:
        from .theme import STATUS_COLORS
        state = await offload(self.plugin.data.latest_status)(self.plugin.id)
        self.model.status = state
        self.model.status_color = STATUS_COLORS.get(state, STATUS_COLORS['offline'])
