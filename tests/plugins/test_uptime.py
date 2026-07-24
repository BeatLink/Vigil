import pytest
from unittest.mock import MagicMock, AsyncMock, patch

pytestmark = pytest.mark.asyncio
from vigil.plugins.uptime import UptimeCollectorPlugin
from vigil.core.data.database import db, StatusHistory, Metric


UPTIME_CFG = {
    "name": "test-uptime",
    "id":   "test-uptime",
    "target_host": "example.host",
    "interval": 30,
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(UptimeCollectorPlugin, UPTIME_CFG)


def _mock_process(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _latest_status(plugin_id: str):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(plugin_name: str, metric: str):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == plugin_name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestUptimeCollection:
    async def test_successful_ping_sets_online(self, plugin, run_local_cycle_async):
        stdout = b"64 bytes from example.host: icmp_seq=1 ttl=64 time=5.2 ms\n"
        proc = _mock_process(0, stdout)
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await run_local_cycle_async(plugin)
        assert _latest_status("test-uptime") == "online"

    async def test_successful_ping_records_latency(self, plugin, run_local_cycle_async):
        stdout = b"64 bytes from 1.2.3.4: icmp_seq=1 ttl=64 time=12.5 ms\n"
        proc = _mock_process(0, stdout)
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await run_local_cycle_async(plugin)
        latency = _latest_metric("test-uptime", "latency_ms")
        assert latency == pytest.approx(12.5)

    async def test_successful_ping_records_up_metric(self, plugin, run_local_cycle_async):
        stdout = b"1 packets transmitted, 1 received, time=1.0 ms\n"
        proc = _mock_process(0, stdout)
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await run_local_cycle_async(plugin)
        assert _latest_metric("test-uptime", "up") == pytest.approx(1.0)

    async def test_failed_ping_sets_failed(self, plugin, run_local_cycle_async):
        proc = _mock_process(1, b"", b"Request timed out")
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await run_local_cycle_async(plugin)
        assert _latest_status("test-uptime") == "failed"

    async def test_failed_ping_records_up_zero(self, plugin, run_local_cycle_async):
        proc = _mock_process(1, b"", b"Network unreachable")
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await run_local_cycle_async(plugin)
        assert _latest_metric("test-uptime", "up") == pytest.approx(0.0)

    async def test_subprocess_exception_sets_failed(self, plugin, run_local_cycle_async):
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   side_effect=OSError("ping not found")):
            await run_local_cycle_async(plugin)
        assert _latest_status("test-uptime") == "failed"

    async def test_no_latency_recorded_on_failure(self, plugin, run_local_cycle_async):
        proc = _mock_process(1, b"", b"")
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await run_local_cycle_async(plugin)
        assert _latest_metric("test-uptime", "latency_ms") is None

    async def test_missing_latency_in_output_not_recorded(self, plugin, run_local_cycle_async):
        stdout = b"1 packets transmitted, 1 received\n"
        proc = _mock_process(0, stdout)
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await run_local_cycle_async(plugin)
        assert _latest_status("test-uptime") == "online"
        assert _latest_metric("test-uptime", "latency_ms") is None


class TestUptimeActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("restart") is None
        assert plugin.plan_action("anything") is None
