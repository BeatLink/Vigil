"""HttpConnector — the HTTP sub-engine of the Connector Engine.

Executes declarative ``HttpRequest`` objects against a single shared,
engine-owned ``httpx.AsyncClient`` (connection pooling, native async — no
thread hop, no executor slot). Plugins never touch the client — they declare
an ``HttpRequest`` and receive an ``HttpResult``.
"""

import asyncio
import time

import httpx

from vigil.core.connectors.types import HttpRequest, HttpResult

_DEFAULT_TIMEOUT = 10.0


class HttpConnector:
    def __init__(self):
        # follow_redirects matches the requests default this client replaced.
        self._client = httpx.AsyncClient(follow_redirects=True)

    async def fetch(self, req: HttpRequest) -> HttpResult:
        timeout = req.timeout if req.timeout is not None else _DEFAULT_TIMEOUT
        started = time.monotonic()
        try:
            resp = await self._client.request(
                req.method, req.url,
                headers=req.headers or None,
                content=req.body,
                auth=tuple(req.auth) if req.auth else None,
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            return HttpResult(status_code=None, text="", error=str(e) or type(e).__name__)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return HttpResult(status_code=resp.status_code, text=resp.text,
                          elapsed_ms=elapsed_ms)

    def close(self) -> None:
        try:
            asyncio.get_running_loop().create_task(self._client.aclose())
        except RuntimeError:
            pass
