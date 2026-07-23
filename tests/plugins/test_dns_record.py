from unittest.mock import patch

import dns.exception
import dns.rdtypes.IN.A
import dns.resolver
import dns.rrset
import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.dns_record import DnsRecordCollectorPlugin
from vigil.core.data.database import db, StatusHistory, Metric


def _latest_status(pid):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(pid, name):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == pid) & (Metric.metric_name == name)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _cfg(**extra):
    base = {"name": "test-dns", "id": "test-dns", "domain": "example.com"}
    base.update(extra)
    return base


class _FakeAnswer:
    """
    Stand in for dns.resolver.Answer: iterable over the rdata records (like
    Answer.__iter__, which delegates to its rrset) and exposes `.rrset` for
    the TTL, matching the two things DnsRecordCollectorPlugin.on_collect reads.
    """
    def __init__(self, rrset):
        self.rrset = rrset

    def __iter__(self):
        return iter(self.rrset)


def _fake_answer(record_type: str, *values: str, ttl: int = 300):
    rrset = dns.rrset.from_text('example.com.', ttl, 'IN', record_type, *values)
    return _FakeAnswer(rrset)


class TestDnsRecordCollection:
    async def test_successful_a_resolution_sets_online(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg())
        answer = _fake_answer('A', '93.184.216.34')
        with patch.object(p, '_query', return_value=answer):
            await p.on_collect()
        assert _latest_status("test-dns") == "online"
        assert _latest_metric("test-dns", "resolved") == pytest.approx(1.0)

    async def test_records_ttl(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg())
        answer = _fake_answer('A', '93.184.216.34', ttl=600)
        with patch.object(p, '_query', return_value=answer):
            await p.on_collect()
        assert _latest_metric("test-dns", "ttl") == pytest.approx(600.0)

    async def test_nxdomain_sets_failed(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg())
        with patch.object(p, '_query', side_effect=dns.resolver.NXDOMAIN()):
            await p.on_collect()
        assert _latest_status("test-dns") == "failed"
        assert _latest_metric("test-dns", "resolved") == pytest.approx(0.0)

    async def test_no_answer_sets_failed(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg(record_type="MX"))
        with patch.object(p, '_query', side_effect=dns.resolver.NoAnswer()):
            await p.on_collect()
        assert _latest_status("test-dns") == "failed"

    async def test_timeout_sets_failed(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg())
        with patch.object(p, '_query', side_effect=dns.exception.Timeout()):
            await p.on_collect()
        assert _latest_status("test-dns") == "failed"

    async def test_generic_dns_exception_sets_failed(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg())
        with patch.object(p, '_query', side_effect=dns.exception.DNSException("bad")):
            await p.on_collect()
        assert _latest_status("test-dns") == "failed"

    async def test_missing_domain_sets_failed(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg(domain=None))
        await p.on_collect()
        assert _latest_status("test-dns") == "failed"


class TestExpectedValues:
    async def test_matching_expected_value_sets_online(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg(expected=["93.184.216.34"]))
        answer = _fake_answer('A', '93.184.216.34')
        with patch.object(p, '_query', return_value=answer):
            await p.on_collect()
        assert _latest_status("test-dns") == "online"
        assert _latest_metric("test-dns", "matches_expected") == pytest.approx(1.0)

    async def test_unexpected_value_sets_failed(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg(expected=["1.2.3.4"]))
        answer = _fake_answer('A', '93.184.216.34')
        with patch.object(p, '_query', return_value=answer):
            await p.on_collect()
        assert _latest_status("test-dns") == "failed"
        assert _latest_metric("test-dns", "matches_expected") == pytest.approx(0.0)

    async def test_no_expected_skips_matching(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg())
        answer = _fake_answer('A', '93.184.216.34')
        with patch.object(p, '_query', return_value=answer):
            await p.on_collect()
        assert _latest_metric("test-dns", "matches_expected") is None


class TestRecordTypeFormatting:
    async def test_mx_record_formatting(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg(record_type="MX", expected=["10 mail.example.com"]))
        answer = _fake_answer('MX', '10 mail.example.com.')
        with patch.object(p, '_query', return_value=answer):
            await p.on_collect()
        assert _latest_status("test-dns") == "online"

    async def test_txt_record_formatting(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg(record_type="TXT", expected=["v=spf1 -all"]))
        answer = _fake_answer('TXT', '"v=spf1 -all"')
        with patch.object(p, '_query', return_value=answer):
            await p.on_collect()
        assert _latest_status("test-dns") == "online"

    async def test_cname_record_strips_trailing_dot(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg(record_type="CNAME", expected=["target.example.net"]))
        answer = _fake_answer('CNAME', 'target.example.net.')
        with patch.object(p, '_query', return_value=answer):
            await p.on_collect()
        assert _latest_status("test-dns") == "online"


class TestDnsRecordActions:
    async def test_on_action_always_returns_false(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg())
        assert await p.on_action("anything") is False


class TestResolverConfig:
    async def test_custom_resolver_is_used(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg(resolver="1.1.1.1", port=5353, timeout=2))
        resolver = p._make_resolver()
        assert resolver.nameservers == ["1.1.1.1"]
        assert resolver.port == 5353
        assert resolver.timeout == 2
        assert resolver.lifetime == 2

    async def test_default_resolver_uses_system_config(self, make_plugin):
        p = make_plugin(DnsRecordCollectorPlugin, _cfg())
        resolver = p._make_resolver()
        assert resolver.port == 53
