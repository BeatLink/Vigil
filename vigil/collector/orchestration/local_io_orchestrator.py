import asyncio
import inspect
from typing import Any, Callable


class LocalIOOrchestrator:
    """Runs an arbitrary zero-arg callable on behalf of a plugin that needs
    local (non-SSH) IO — e.g. DNS resolution, outbound HTTP, a local
    subprocess like ping. Keeps the 'plugin never awaits' rule intact for
    plugins that don't talk to the monitored host over SSH at all.

    Blocking callables run off-thread via asyncio.to_thread(); async
    callables (coroutine functions, e.g. ones wrapping
    asyncio.create_subprocess_exec) are awaited directly."""

    async def run(self, fn: Callable[[], Any]) -> Any:
        if inspect.iscoroutinefunction(fn):
            return await fn()
        return await asyncio.to_thread(fn)
