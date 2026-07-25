import asyncio
from typing import Any, Dict, List

from nicegui import binding, context, helpers
from nicegui import Client

from vigil.core.contracts import RefreshCallback
from .components import safe_timer, offload

_schedulers: Dict[str, "_PageScheduler"] = {}


class _PageScheduler:
    """One timer per browser client, ticking every registered *tickable* — a
    PluginPage or a bare callback (via _CallbackTick). A tickable is anything
    with ``_tick()`` (async) and ``_detached()``. Coordinating every card,
    chart, table and overview refresh for a client onto a single timer avoids
    the fan of independent per-widget timers the UI used to spin up."""

    def __init__(self, client_id: str, interval: float):
        self._client_id = client_id
        self._interval = interval
        self._tickables: List[Any] = []
        self._timer = None

    def add(self, tickable: Any) -> None:
        self._tickables.append(tickable)
        if self._timer is None:
            self._timer = safe_timer(self._interval, self._tick, defer_first=True)
        elif self._interval > tickable._interval:
            self._interval = tickable._interval
            self._timer.cancel()
            self._timer = safe_timer(self._interval, self._tick, defer_first=True)
        asyncio.create_task(tickable._tick())

    async def _tick(self) -> None:
        live = [t for t in self._tickables if not t._detached()]
        self._tickables = live
        if not live:
            if self._timer is not None:
                self._timer.cancel()
            _schedulers.pop(self._client_id, None)
            return
        await asyncio.gather(*(t._tick() for t in live))


class _CallbackTick:
    """Adapts a bare refresh callback to the scheduler's tickable protocol so
    overview-page refreshes (on_data_event) share the client's single timer
    instead of each owning an independent safe_timer. Detachment is tied to the
    client the callback was registered under — same lifetime as a PluginPage."""

    def __init__(self, callback: RefreshCallback, interval: float, run_now: bool):
        self._callback = callback
        self._interval = interval
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


def _scheduler_for_current_client(interval: float) -> _PageScheduler:
    client = context.client
    sched = _schedulers.get(client.id)
    if sched is None:
        sched = _PageScheduler(client.id, interval)
        _schedulers[client.id] = sched
    return sched


def schedule_callback(callback: RefreshCallback, interval: float = 1.0,
                      run_now: bool = True) -> None:
    """Register a plain refresh callback on the current client's shared
    scheduler. The single entry point the overview page's on_data_event uses so
    its refreshes ride the same timer as everything else on the page."""
    _scheduler_for_current_client(interval).add(
        _CallbackTick(callback, interval, run_now))


@binding.bindable_dataclass
class PluginModel:
    status: str = 'offline'
    status_color: str = ''
    metrics: Dict[str, Any] = None

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}


class PluginPage:
    def __init__(self, plugin: Any, metric_names: List[str] = (),
                interval: float = 1.0):
        self.plugin = plugin
        self.model = PluginModel()
        self._metric_names = list(metric_names)
        self._refresh_callbacks: List[RefreshCallback] = []
        self._interval = interval
        self._client = None

    def on_refresh(self, callback: RefreshCallback) -> None:
        self._refresh_callbacks.append(callback)

    def track_metric(self, name: str) -> None:
        if name not in self._metric_names:
            self._metric_names.append(name)

    def start(self) -> None:
        self._client = context.client
        _scheduler_for_current_client(self._interval).add(self)

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
        state = await offload(self.plugin.data.latest_status_cached)(self.plugin.id)
        self.model.status = state
        self.model.status_color = STATUS_COLORS.get(state, STATUS_COLORS['offline'])
