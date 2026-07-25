"""Connector Engine.

Handles all IO with external sources on behalf of pure plugins. It owns two
named sub-engines — the SSH connector (``connectors/ssh/``) and the HTTP
connector (``connectors/http/``, which also covers DNS and ICMP) — and routes
a plugin's declared, heterogeneous request list to the right one.

A plugin declares ``requests() -> List[Request]`` (a mix of ``Command`` /
``HttpRequest`` / ``DnsQuery`` / ``PingRequest``) and consumes a
positionally-matched ``List[Result]`` in its pure ``parse_results()``. SSH
``Command``s run through the plugin's per-target ``NetworkOrchestrator`` (the
pooled asyncssh connection); the other kinds run through engine-owned shared
sub-connectors.
"""

import asyncio
from typing import List, Optional

from vigil.core.connectors.types import (
    Command, DnsQuery, HttpRequest, PingRequest, Request, Result,
)


class ConnectorEngine:
    def __init__(self):
        # Lazily built so importing this package doesn't require requests /
        # dnspython until the HTTP/DNS/ICMP paths are actually used.
        self._http = None
        self._dns = None
        self._icmp = None

    @property
    def http(self):
        if self._http is None:
            from vigil.core.connectors.http.http_connector import HttpConnector
            self._http = HttpConnector()
        return self._http

    @property
    def dns(self):
        if self._dns is None:
            from vigil.core.connectors.http.dns_connector import DnsConnector
            self._dns = DnsConnector()
        return self._dns

    @property
    def icmp(self):
        if self._icmp is None:
            from vigil.core.connectors.http.icmp_connector import IcmpConnector
            self._icmp = IcmpConnector()
        return self._icmp

    async def run(self, plugin_net, requests: List[Request]) -> List[Result]:
        """Route each request to its sub-connector and gather the results,
        preserving order so parse_results() can match by position.

        ``plugin_net`` is the plugin's SSH ``NetworkOrchestrator`` (needed only
        for ``Command`` requests); pass None for plugins that issue no SSH."""

        async def _one(req: Request) -> Result:
            if isinstance(req, Command):
                return (await plugin_net.run([req]))[0]
            if isinstance(req, HttpRequest):
                return await self.http.fetch(req)
            if isinstance(req, DnsQuery):
                return await self.dns.resolve(req)
            if isinstance(req, PingRequest):
                return await self.icmp.ping(req)
            raise TypeError(f"Unroutable request type: {type(req).__name__}")

        return list(await asyncio.gather(*(_one(r) for r in requests)))

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
