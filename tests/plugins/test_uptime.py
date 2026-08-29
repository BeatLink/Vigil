import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.uptime import Uptime
from vigil.core.connectors.types import PingResult
from vigil.core.database.database import db, StatusHistory, Metric


UPTIME_CFG = {
    "name": "test-uptime",
    "id":   "test-uptime",
    "target_host": "example.host",
    "interval": 30,
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Uptime, UPTIME_CFG)


def _ping(returncode, stdout="", stderr="", exception=None):
    """Build the PingResult the IcmpConnector would hand parse_results()."""
    return lambda _req: PingResult(
        exception=exception, returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _latest_status(plugin_id: str):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(plugin_name: str, metric: str):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == plugin_name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestUptimeCollection:
    async def test_successful_ping_sets_online(self, plugin, run_requests):
        stdout = "64 bytes from example.host: icmp_seq=1 ttl=64 time=5.2 ms\n"
        run_requests(plugin, _ping(0, stdout))
        assert _latest_status("test-uptime") == "online"

    async def test_successful_ping_records_latency(self, plugin, run_requests):
        stdout = "64 bytes from 1.2.3.4: icmp_seq=1 ttl=64 time=12.5 ms\n"
        run_requests(plugin, _ping(0, stdout))
        latency = _latest_metric("test-uptime", "latency_ms")
        assert latency == pytest.approx(12.5)

    async def test_successful_ping_records_up_metric(self, plugin, run_requests):
        stdout = "1 packets transmitted, 1 received, time=1.0 ms\n"
        run_requests(plugin, _ping(0, stdout))
        assert _latest_metric("test-uptime", "up") == pytest.approx(1.0)

    async def test_failed_ping_sets_failed(self, plugin, run_requests):
        run_requests(plugin, _ping(1, stderr="Request timed out"))
        assert _latest_status("test-uptime") == "failed"

    async def test_failed_ping_records_up_zero(self, plugin, run_requests):
        run_requests(plugin, _ping(1, stderr="Network unreachable"))
        assert _latest_metric("test-uptime", "up") == pytest.approx(0.0)

    async def test_subprocess_exception_sets_failed(self, plugin, run_requests):
        run_requests(plugin, _ping(None, exception="ping not found"))
        assert _latest_status("test-uptime") == "failed"

    async def test_no_latency_recorded_on_failure(self, plugin, run_requests):
        run_requests(plugin, _ping(1))
        assert _latest_metric("test-uptime", "latency_ms") is None

    async def test_missing_latency_in_output_not_recorded(self, plugin, run_requests):
        run_requests(plugin, _ping(0, "1 packets transmitted, 1 received\n"))
        assert _latest_status("test-uptime") == "online"
        assert _latest_metric("test-uptime", "latency_ms") is None


class TestUptimeActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("restart") is None
        assert plugin.plan_action("anything") is None
