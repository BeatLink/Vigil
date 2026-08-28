"""DnsConnector — the DNS sub-engine of the Connector Engine.

Resolves declarative ``DnsQuery`` objects via dnspython's async resolver, on
the event loop. Plugins never touch ``dns.resolver`` — they declare a
``DnsQuery`` and receive a ``DnsResult`` whose ``answer`` (on success) is the
raw dnspython Answer, so a plugin's pure ``parse_results`` keeps full rdata
access (MX preference, TXT strings, rrset TTL).
"""

from typing import Dict, Optional, Tuple

import dns.asyncresolver
import dns.exception
import dns.resolver

from vigil.core.connectors.types import DnsQuery, DnsResult


class DnsConnector:
    def __init__(self):
        # One resolver per (nameserver, port), so /etc/resolv.conf is not
        # re-read on every poll of every DNS monitor.
        self._resolvers: Dict[Tuple[Optional[str], int], dns.asyncresolver.Resolver] = {}

    def _resolver_for(self, query: DnsQuery) -> dns.asyncresolver.Resolver:
        key = (query.resolver, query.port)
        resolver = self._resolvers.get(key)
        if resolver is None:
            resolver = dns.asyncresolver.Resolver(configure=query.resolver is None)
            if query.resolver:
                resolver.nameservers = [query.resolver]
            resolver.port = query.port
            self._resolvers[key] = resolver
        return resolver

    async def resolve(self, query: DnsQuery) -> DnsResult:
        resolver = self._resolver_for(query)
        try:
            answer = await resolver.resolve(query.domain, query.record_type,
                                            lifetime=query.timeout)
            return DnsResult(kind='ok', answer=answer)
        except dns.resolver.NXDOMAIN:
            return DnsResult(kind='nxdomain')
        except dns.resolver.NoAnswer:
            return DnsResult(kind='no_answer')
        except dns.exception.Timeout:
            return DnsResult(kind='timeout')
        except dns.exception.DNSException as e:
            return DnsResult(kind='dns_error', error=str(e))
