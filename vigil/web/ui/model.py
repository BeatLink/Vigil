import asyncio
from typing import Any, Callable, Dict, List

from nicegui import binding, context, helpers
from nicegui import Client

from .components import safe_timer, offload

_schedulers: Dict[str, "_PageScheduler"] = {}


class _PageScheduler:
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
            self._interval = page._interval
            self._timer.cancel()
            self._timer = safe_timer(self._interval, self._tick, defer_first=True)

    async def _tick(self) -> None:
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
        self._refresh_callbacks: List[Callable[[], None]] = []
        self._interval = interval
        self._client = None

    def on_refresh(self, callback: Callable[[], None]) -> None:
        self._refresh_callbacks.append(callback)

    def track_metric(self, name: str) -> None:
        if name not in self._metric_names:
            self._metric_names.append(name)

    def start(self) -> None:
        self._refresh_model()
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
        from .theme import STATUS_COLORS
        state = await offload(self.plugin.db.latest_status_cached)(self.plugin.id)
        self.model.status = state
        self.model.status_color = STATUS_COLORS.get(state, STATUS_COLORS['offline'])
