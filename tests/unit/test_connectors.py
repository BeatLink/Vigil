"""Connector Engine sub-connector tests: the IO logic moved out of the
former local_call() plugins now lives in HttpConnector/DnsConnector/
IcmpConnector, so its behavior is verified here at the connector level."""

from unittest.mock import MagicMock, patch

import dns.exception
import dns.resolver
import pytest

from vigil.core.connectors.types import (
    Command, CmdResult, DnsQuery, HttpRequest, PingRequest,
)

pytestmark = pytest.mark.asyncio


class TestDnsConnector:
    async def test_custom_resolver_config_applied(self):
        from vigil.core.connectors.dns_connector import DnsConnector

        captured = {}

        class FakeResolver:
            def __init__(self, configure=True):
                captured['configure'] = configure
                self.nameservers = []
                self.port = 53
                self.timeout = None
                self.lifetime = None

            def resolve(self, domain, rtype):
                captured['domain'] = domain
                captured['rtype'] = rtype
                captured['nameservers'] = self.nameservers
                captured['port'] = self.port
                captured['timeout'] = self.timeout
                return MagicMock()

        with patch('dns.resolver.Resolver', FakeResolver):
            result = await DnsConnector().resolve(
                DnsQuery('example.com', 'A', resolver='1.1.1.1', port=5353, timeout=2)
            )
        assert result.kind == 'ok'
        assert captured['configure'] is False
        assert captured['nameservers'] == ['1.1.1.1']
        assert captured['port'] == 5353
        assert captured['timeout'] == 2

    async def test_default_resolver_is_system_configured(self):
        from vigil.core.connectors.dns_connector import DnsConnector

        captured = {}

        class FakeResolver:
            def __init__(self, configure=True):
                captured['configure'] = configure
                self.nameservers = []
                self.port = 53
                self.timeout = None
                self.lifetime = None

            def resolve(self, domain, rtype):
                return MagicMock()

        with patch('dns.resolver.Resolver', FakeResolver):
            await DnsConnector().resolve(DnsQuery('example.com', 'A'))
        assert captured['configure'] is True

    @pytest.mark.parametrize('exc,kind', [
        (dns.resolver.NXDOMAIN, 'nxdomain'),
        (dns.resolver.NoAnswer, 'no_answer'),
        (dns.exception.Timeout, 'timeout'),
    ])
    async def test_dns_exceptions_mapped_to_kind(self, exc, kind):
        from vigil.core.connectors.dns_connector import DnsConnector

        class FakeResolver:
            def __init__(self, configure=True):
                self.nameservers = []
                self.port = 53
                self.timeout = None
                self.lifetime = None

            def resolve(self, domain, rtype):
                raise exc()

        with patch('dns.resolver.Resolver', FakeResolver):
            result = await DnsConnector().resolve(DnsQuery('example.com', 'A'))
        assert result.kind == kind

    async def test_generic_dns_error_carries_message(self):
        from vigil.core.connectors.dns_connector import DnsConnector

        class FakeResolver:
            def __init__(self, configure=True):
                self.nameservers = []
                self.port = 53
                self.timeout = None
                self.lifetime = None

            def resolve(self, domain, rtype):
                raise dns.exception.DNSException('boom')

        with patch('dns.resolver.Resolver', FakeResolver):
            result = await DnsConnector().resolve(DnsQuery('example.com', 'A'))
        assert result.kind == 'dns_error'
        assert 'boom' in (result.error or '')


class TestHttpConnector:
    async def test_get_returns_status_and_text(self):
        from vigil.core.connectors.http_connector import HttpConnector

        conn = HttpConnector()
        resp = MagicMock(status_code=200, text='hello')
        with patch.object(conn._session, 'request', return_value=resp) as req:
            result = await conn.fetch(HttpRequest('https://x/', timeout=3))
        assert result.status_code == 200
        assert result.text == 'hello'
        assert result.error is None
        req.assert_called_once()

    async def test_request_exception_becomes_error(self):
        import requests as _requests
        from vigil.core.connectors.http_connector import HttpConnector

        conn = HttpConnector()
        with patch.object(conn._session, 'request',
                          side_effect=_requests.RequestException('no route')):
            result = await conn.fetch(HttpRequest('https://x/'))
        assert result.status_code is None
        assert 'no route' in (result.error or '')
        assert result.elapsed_ms == 0.0

    async def test_basic_auth_and_method_body_headers_forwarded(self):
        from vigil.core.connectors.http_connector import HttpConnector

        conn = HttpConnector()
        resp = MagicMock(status_code=207, text='<multistatus/>')
        with patch.object(conn._session, 'request', return_value=resp) as req:
            await conn.fetch(HttpRequest(
                'https://dav/', method='PROPFIND',
                headers={'Depth': '0'}, body='<propfind/>',
                auth=('user', 'pw'), timeout=4))
        _, kwargs = req.call_args
        assert kwargs['auth'] == ('user', 'pw')
        assert kwargs['headers'] == {'Depth': '0'}
        assert kwargs['data'] == '<propfind/>'

    async def test_elapsed_ms_measured_on_success(self):
        from vigil.core.connectors.http_connector import HttpConnector

        conn = HttpConnector()
        resp = MagicMock(status_code=200, text='ok')
        with patch.object(conn._session, 'request', return_value=resp):
            result = await conn.fetch(HttpRequest('https://x/'))
        assert result.elapsed_ms >= 0.0


class TestIcmpConnector:
    async def test_successful_ping(self):
        from vigil.core.connectors.icmp_connector import IcmpConnector

        proc = MagicMock(returncode=0)

        async def _communicate():
            return (b'time=5.2 ms', b'')

        proc.communicate = _communicate
        with patch('asyncio.create_subprocess_exec', return_value=proc):
            result = await IcmpConnector().ping(PingRequest('host'))
        assert result.exception is None
        assert result.returncode == 0
        assert 'time=5.2 ms' in result.stdout

    async def test_subprocess_exception_captured(self):
        from vigil.core.connectors.icmp_connector import IcmpConnector

        with patch('asyncio.create_subprocess_exec', side_effect=OSError('ping missing')):
            result = await IcmpConnector().ping(PingRequest('host'))
        assert result.returncode is None
        assert 'ping missing' in (result.exception or '')


class TestConnectorEngineRouting:
    async def test_routes_command_to_ssh_network(self):
        from vigil.core.connectors import ConnectorEngine, SSHContext
        from unittest.mock import AsyncMock

        engine = ConnectorEngine()
        conn = MagicMock()
        conn.host = 'test.host'
        conn.execute = AsyncMock(return_value=(0, 'out', ''))
        ctx = SSHContext(conn=conn, collect_timeout=30.0)
        results = await engine.run(ctx, [Command('echo hi')])
        assert results[0].stdout == 'out'
        conn.execute.assert_awaited_once()

    async def test_routes_by_type(self):
        from vigil.core.connectors import ConnectorEngine
        from vigil.core.connectors.types import HttpResult, DnsResult, PingResult
        from unittest.mock import AsyncMock

        engine = ConnectorEngine()
        engine._http = MagicMock(fetch=AsyncMock(return_value=HttpResult(200, 'ok')))
        engine._dns = MagicMock(resolve=AsyncMock(return_value=DnsResult('ok')))
        engine._icmp = MagicMock(ping=AsyncMock(return_value=PingResult(None, 0)))

        results = await engine.run(None, [
            HttpRequest('https://x/'), DnsQuery('d'), PingRequest('h'),
        ])
        assert isinstance(results[0], HttpResult)
        assert isinstance(results[1], DnsResult)
        assert isinstance(results[2], PingResult)
