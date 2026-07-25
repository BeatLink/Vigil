"""HttpConnector — the HTTP sub-engine of the Connector Engine.

Executes declarative ``HttpRequest`` objects against a single shared,
engine-owned ``requests.Session`` (connection pooling), off the event loop via
``asyncio.to_thread``. Plugins never touch ``requests`` — they declare an
``HttpRequest`` and receive an ``HttpResult``.
"""

import asyncio
import time
from typing import Optional
import requests
from vigil.core.connectors.types import HttpRequest, HttpResult

_DEFAULT_TIMEOUT = 10.0


class HttpConnector:
    def __init__(self):
        self._session = requests.Session()

    async def fetch(self, req: HttpRequest) -> HttpResult:
        return await asyncio.to_thread(self._fetch_sync, req)

    def _fetch_sync(self, req: HttpRequest) -> HttpResult:
        timeout = req.timeout if req.timeout is not None else _DEFAULT_TIMEOUT
        started = time.monotonic()
        try:
            resp = self._session.request(
                req.method, req.url,
                headers=req.headers or None,
                data=req.body,
                auth=tuple(req.auth) if req.auth else None,
                timeout=timeout,
            )
        except requests.RequestException as e:
            return HttpResult(status_code=None, text="", error=str(e))
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return HttpResult(status_code=resp.status_code, text=resp.text,
                          elapsed_ms=elapsed_ms)

    def close(self) -> None:
        self._session.close()
