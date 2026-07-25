"""DnsConnector — the DNS sub-engine of the Connector Engine.

Resolves declarative ``DnsQuery`` objects via dnspython, off the event loop.
Plugins never touch ``dns.resolver`` — they declare a ``DnsQuery`` and receive
a ``DnsResult`` whose ``answer`` (on success) is the raw dnspython Answer, so a
plugin's pure ``parse_results`` keeps full rdata access (MX preference, TXT
strings, rrset TTL).
"""

import asyncio

import dns.exception
import dns.resolver

from vigil.core.connectors.types import DnsQuery, DnsResult


class DnsConnector:
    async def resolve(self, query: DnsQuery) -> DnsResult:
        return await asyncio.to_thread(self._resolve_sync, query)

    def _resolve_sync(self, query: DnsQuery) -> DnsResult:
        resolver = dns.resolver.Resolver(configure=query.resolver is None)
        if query.resolver:
            resolver.nameservers = [query.resolver]
        resolver.port = query.port
        resolver.timeout = query.timeout
        resolver.lifetime = query.timeout
        try:
            answer = resolver.resolve(query.domain, query.record_type)
            return DnsResult(kind='ok', answer=answer)
        except dns.resolver.NXDOMAIN:
            return DnsResult(kind='nxdomain')
        except dns.resolver.NoAnswer:
            return DnsResult(kind='no_answer')
        except dns.exception.Timeout:
            return DnsResult(kind='timeout')
        except dns.exception.DNSException as e:
            return DnsResult(kind='dns_error', error=str(e))
