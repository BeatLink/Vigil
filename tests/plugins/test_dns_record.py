import dns.rrset
import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.dns_record import DnsRecord
from vigil.core.connectors.types import DnsQuery, DnsResult
from vigil.core.database.database import db, StatusHistory, Metric


def _resolves(answer):
    """Build the DnsResult the DnsConnector would hand parse_results()."""
    return lambda _req: DnsResult(kind='ok', answer=answer)


def _fails(kind, error=None):
    return lambda _req: DnsResult(kind=kind, error=error)


def _latest_status(pid):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(pid, name):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == pid) & (Metric.metric_name == name)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _cfg(**extra):
    base = {"name": "test-dns", "id": "test-dns", "domain": "example.com"}
    base.update(extra)
    return base


class _FakeAnswer:
    def __init__(self, rrset):
        self.rrset = rrset

    def __iter__(self):
        return iter(self.rrset)


def _fake_answer(record_type: str, *values: str, ttl: int = 300):
    rrset = dns.rrset.from_text('example.com.', ttl, 'IN', record_type, *values)
    return _FakeAnswer(rrset)


class TestDnsRecordCollection:
    async def test_successful_a_resolution_sets_online(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg())
        run_requests(p, _resolves(_fake_answer('A', '93.184.216.34')))
        assert _latest_status("test-dns") == "online"
        assert _latest_metric("test-dns", "resolved") == pytest.approx(1.0)

    async def test_records_ttl(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg())
        run_requests(p, _resolves(_fake_answer('A', '93.184.216.34', ttl=600)))
        assert _latest_metric("test-dns", "ttl") == pytest.approx(600.0)

    async def test_nxdomain_sets_failed(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg())
        run_requests(p, _fails('nxdomain'))
        assert _latest_status("test-dns") == "failed"
        assert _latest_metric("test-dns", "resolved") == pytest.approx(0.0)

    async def test_no_answer_sets_failed(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg(record_type="MX"))
        run_requests(p, _fails('no_answer'))
        assert _latest_status("test-dns") == "failed"

    async def test_timeout_sets_failed(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg())
        run_requests(p, _fails('timeout'))
        assert _latest_status("test-dns") == "failed"

    async def test_generic_dns_exception_sets_failed(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg())
        run_requests(p, _fails('dns_error', error='bad'))
        assert _latest_status("test-dns") == "failed"

    async def test_missing_domain_sets_failed(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg(domain=None))
        run_requests(p)  # no domain -> requests() == [] -> parse_results([])
        assert _latest_status("test-dns") == "failed"


class TestExpectedValues:
    async def test_matching_expected_value_sets_online(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg(expected=["93.184.216.34"]))
        run_requests(p, _resolves(_fake_answer('A', '93.184.216.34')))
        assert _latest_status("test-dns") == "online"
        assert _latest_metric("test-dns", "matches_expected") == pytest.approx(1.0)

    async def test_unexpected_value_sets_failed(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg(expected=["1.2.3.4"]))
        run_requests(p, _resolves(_fake_answer('A', '93.184.216.34')))
        assert _latest_status("test-dns") == "failed"
        assert _latest_metric("test-dns", "matches_expected") == pytest.approx(0.0)

    async def test_no_expected_skips_matching(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg())
        run_requests(p, _resolves(_fake_answer('A', '93.184.216.34')))
        assert _latest_metric("test-dns", "matches_expected") is None


class TestRecordTypeFormatting:
    async def test_mx_record_formatting(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg(record_type="MX", expected=["10 mail.example.com"]))
        run_requests(p, _resolves(_fake_answer('MX', '10 mail.example.com.')))
        assert _latest_status("test-dns") == "online"

    async def test_txt_record_formatting(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg(record_type="TXT", expected=["v=spf1 -all"]))
        run_requests(p, _resolves(_fake_answer('TXT', '"v=spf1 -all"')))
        assert _latest_status("test-dns") == "online"

    async def test_cname_record_strips_trailing_dot(self, make_plugin, run_requests):
        p = make_plugin(DnsRecord, _cfg(record_type="CNAME", expected=["target.example.net"]))
        run_requests(p, _resolves(_fake_answer('CNAME', 'target.example.net.')))
        assert _latest_status("test-dns") == "online"


class TestDnsRecordActions:
    async def test_on_action_always_returns_false(self, make_plugin):
        p = make_plugin(DnsRecord, _cfg())
        assert p.plan_action("anything") is None


class TestDnsQueryDeclaration:
    """The plugin declares a DnsQuery; the DnsConnector owns resolution."""

    async def test_custom_resolver_in_query(self, make_plugin):
        p = make_plugin(DnsRecord, _cfg(resolver="1.1.1.1", port=5353, timeout=2))
        query = p.requests()[0]
        assert isinstance(query, DnsQuery)
        assert query.resolver == "1.1.1.1"
        assert query.port == 5353
        assert query.timeout == 2

    async def test_default_query_uses_port_53(self, make_plugin):
        p = make_plugin(DnsRecord, _cfg())
        query = p.requests()[0]
        assert query.port == 53
        assert query.resolver is None

    async def test_no_domain_emits_no_query(self, make_plugin):
        p = make_plugin(DnsRecord, _cfg(domain=None))
        assert p.requests() == []
