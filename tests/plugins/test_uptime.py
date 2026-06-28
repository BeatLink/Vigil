import pytest
from unittest.mock import MagicMock, AsyncMock, patch

pytestmark = pytest.mark.asyncio
from vigil.plugins.uptime import UptimePlugin
from vigil.core.data.database import db, StatusHistory, Metric


UPTIME_CFG = {
    "name": "test-uptime",
    "id":   "test-uptime",
    "target_host": "example.host",
    "interval": 30,
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(UptimePlugin, UPTIME_CFG)


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
    async def test_successful_ping_sets_online(self, plugin):
        stdout = b"64 bytes from example.host: icmp_seq=1 ttl=64 time=5.2 ms\n"
        proc = _mock_process(0, stdout)
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await plugin.on_collect()
        assert _latest_status("test-uptime") == "online"

    async def test_successful_ping_records_latency(self, plugin):
        stdout = b"64 bytes from 1.2.3.4: icmp_seq=1 ttl=64 time=12.5 ms\n"
        proc = _mock_process(0, stdout)
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await plugin.on_collect()
        latency = _latest_metric("test-uptime", "latency_ms")
        assert latency == pytest.approx(12.5)

    async def test_successful_ping_records_up_metric(self, plugin):
        stdout = b"1 packets transmitted, 1 received, time=1.0 ms\n"
        proc = _mock_process(0, stdout)
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await plugin.on_collect()
        assert _latest_metric("test-uptime", "up") == pytest.approx(1.0)

    async def test_failed_ping_sets_failed(self, plugin):
        proc = _mock_process(1, b"", b"Request timed out")
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await plugin.on_collect()
        assert _latest_status("test-uptime") == "failed"

    async def test_failed_ping_records_up_zero(self, plugin):
        proc = _mock_process(1, b"", b"Network unreachable")
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await plugin.on_collect()
        assert _latest_metric("test-uptime", "up") == pytest.approx(0.0)

    async def test_subprocess_exception_sets_failed(self, plugin):
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   side_effect=OSError("ping not found")):
            await plugin.on_collect()
        assert _latest_status("test-uptime") == "failed"

    async def test_no_latency_recorded_on_failure(self, plugin):
        proc = _mock_process(1, b"", b"")
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await plugin.on_collect()
        # latency_ms should not be written on failure
        assert _latest_metric("test-uptime", "latency_ms") is None

    async def test_missing_latency_in_output_not_recorded(self, plugin):
        stdout = b"1 packets transmitted, 1 received\n"  # no time= field
        proc = _mock_process(0, stdout)
        with patch("vigil.plugins.uptime.asyncio.create_subprocess_exec",
                   AsyncMock(return_value=proc)):
            await plugin.on_collect()
        # status is online, but no latency metric
        assert _latest_status("test-uptime") == "online"
        assert _latest_metric("test-uptime", "latency_ms") is None


class TestUptimeActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("restart") is False
        assert await plugin.on_action("anything") is False
