import asyncio
import logging
from typing import Callable, Dict, List, Optional


class DataBus:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[], None]]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.polling_mode: bool = False

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def on(self, event: str, callback: Callable[[], None]) -> Callable[[], None]:
        subs = self._subscribers.setdefault(event, [])
        subs.append(callback)

        def off():
            try:
                subs.remove(callback)
            except ValueError:
                pass

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
