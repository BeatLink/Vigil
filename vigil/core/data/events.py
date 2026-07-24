import asyncio
import logging
from typing import Callable, Dict, List, Optional


class DataBus:
    """
    Process-wide "something changed" notifications, so widgets can subscribe
    instead of polling on a timer.

    Events are per data type (`status`, `metric`, `event`, `log_line`,
    `setting`), not per monitor id: a widget re-checks on any write of that
    type and filters
    client-side, same as `latest_statuses()` already does as one shared
    query — this avoids every widget having to subscribe/unsubscribe by id as
    it mounts and unmounts.

    `emit()` is called from `_AsyncWriter`'s background thread, right after a
    write batch commits (not at insert() time — the row isn't queryable
    until the batch actually lands, so notifying earlier would have widgets
    re-query and see stale/missing data). Callbacks always need to run on the
    UI's asyncio event loop, not the writer thread, so `emit()` hands off via
    `call_soon_threadsafe` once `bind_loop()` has been called with the
    running loop (done once at startup, from `VigilEngine.run()`).
    Before `bind_loop()` — e.g. early in tests, or before the UI has started —
    emit() is a no-op rather than raising, since there's nothing subscribed
    yet anyway.

    In the web process (see the collector/web process split), no writer
    thread ever runs here — writes happen only in the collector process, so
    this bus never emits. `polling_mode` is set there instead, and
    `on_data_event` (components.py) checks it to fall back to a short timer
    per widget rather than subscribing to an emit() that will never come.
    """
    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[], None]]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # True in the web process: writes (and therefore emit()) happen only
        # in the collector process, so widgets must poll instead of
        # subscribing. See on_data_event in components.py.
        self.polling_mode: bool = False

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def on(self, event: str, callback: Callable[[], None]) -> Callable[[], None]:
        """
        Subscribe to an event type. Returns an `off()` function that removes
        this subscription — callers whose widget can be torn down (i.e. every
        UI widget; see on_data_event in components.py) must call it once the
        widget is gone, or the callback (and everything its closure holds:
        the table/chart element, query params, ...) leaks for the rest of the
        process's life, since nothing else ever removes it from this list.
        """
        subs = self._subscribers.setdefault(event, [])
        subs.append(callback)

        def off():
            try:
                subs.remove(callback)
            except ValueError:
                pass  # already removed

        return off

    def emit(self, event: str):
        loop = self._loop
        if loop is None:
            return
        callbacks = list(self._subscribers.get(event, ()))
        if not callbacks:
            return

        def _run():
            import asyncio
            for cb in callbacks:
                try:
                    result = cb()
                    if asyncio.iscoroutine(result):
                        asyncio.ensure_future(result)
                except Exception as e:
                    logging.error(f"DataBus callback failed for '{event}': {e}")

        loop.call_soon_threadsafe(_run)


bus = DataBus()
