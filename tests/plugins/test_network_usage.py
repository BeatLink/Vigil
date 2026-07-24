import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.network_usage import NetworkUsageCollectorPlugin, _parse_net_dev, _auto_detect_interface, _format_rate
from vigil.collector.orchestration.types import CmdResult
from vigil.core.data.database import db, StatusHistory, Metric


NET_DEV_HEADER = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
)

BASE_CFG = {
    "name": "test-net",
    "id":   "test-net",
    "ssh_config": {"host": "test.host"},
}


def _make_net_dev(ifaces: dict) -> str:
    lines = [NET_DEV_HEADER]
    for iface, (rx, tx) in ifaces.items():
        lines.append(f"  {iface}: {rx} 100 0 0 0 0 0 0 {tx} 50 0 0 0 0 0 0\n")
    return "".join(lines)


def _two_snapshots(ifaces1: dict, ifaces2: dict) -> str:
    return _make_net_dev(ifaces1) + _make_net_dev(ifaces2)


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(NetworkUsageCollectorPlugin, BASE_CFG)


@pytest.fixture
def explicit_plugin(make_plugin):
    return make_plugin(NetworkUsageCollectorPlugin, {
        "name": "test-net-explicit",
        "id": "test-net-explicit",
        "interface": "eth0",
        "ssh_config": {"host": "test.host"},
    })


def _latest_status(plugin_id: str = "test-net"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(name: str, metric: str):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestParseNetDev:
    def test_parses_rx_and_tx(self):
        block = _make_net_dev({"eth0": (1024, 512)})
        result = _parse_net_dev(block)
        assert result["eth0"] == (1024, 512)

    def test_parses_multiple_interfaces(self):
        block = _make_net_dev({"eth0": (1000, 200), "lo": (50, 50)})
        result = _parse_net_dev(block)
        assert "eth0" in result
        assert "lo" in result

    def test_skips_header_lines(self):
        block = _make_net_dev({"eth0": (0, 0)})
        result = _parse_net_dev(block)
        assert "Inter" not in result
        assert "face" not in result

    def test_strips_whitespace_from_iface_name(self):
        block = "   eth0: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\n"
        result = _parse_net_dev(block)
        assert "eth0" in result


class TestAutoDetect:
    def test_picks_highest_traffic_interface(self):
        stats = {"eth0": (1_000_000, 500_000), "wlan0": (100, 50)}
        assert _auto_detect_interface(stats) == "eth0"

    def test_excludes_loopback(self):
        stats = {"lo": (9_999_999, 9_999_999), "eth0": (1000, 500)}
        assert _auto_detect_interface(stats) == "eth0"

    def test_excludes_virtual_prefixes(self):
        stats = {
            "veth0":   (9_999_999, 0),
            "docker0": (9_999_999, 0),
            "virbr0":  (9_999_999, 0),
            "br-abc":  (9_999_999, 0),
            "tun0":    (9_999_999, 0),
            "tap0":    (9_999_999, 0),
            "eth0":    (1000, 500),
        }
        assert _auto_detect_interface(stats) == "eth0"

    def test_returns_none_when_no_candidates(self):
        stats = {"lo": (100, 100), "veth0": (200, 200)}
        assert _auto_detect_interface(stats) is None


class TestFormatRate:
    def test_below_1024_shows_kbps(self):
        assert _format_rate(512.0) == "512.0 KB/s"

    def test_at_1024_shows_mbps(self):
        assert _format_rate(1024.0) == "1.0 MB/s"

    def test_above_1024_shows_mbps(self):
        assert _format_rate(2048.0) == "2.0 MB/s"


class TestNetworkUsageCollection:
    async def test_normal_collection_sets_online(self, plugin, run_cycle):
        stdout = _two_snapshots(
            {"lo": (0, 0), "eth0": (1_000_000, 500_000)},
            {"lo": (0, 0), "eth0": (1_001_024, 500_512)},
        )
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "online"

    async def test_rx_metric_recorded(self, plugin, run_cycle):
        stdout = _two_snapshots(
            {"eth0": (0, 0)},
            {"eth0": (1024, 0)},
        )
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_metric("test-net", "rx_kbps") == pytest.approx(1.0)

    async def test_tx_metric_recorded(self, plugin, run_cycle):
        stdout = _two_snapshots(
            {"eth0": (0, 0)},
            {"eth0": (0, 2048)},
        )
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_metric("test-net", "tx_kbps") == pytest.approx(2.0)

    async def test_auto_detects_busiest_interface(self, plugin, run_cycle):
        stdout = _two_snapshots(
            {"lo": (0, 0), "eth0": (1_000_000, 0), "wlan0": (100, 0)},
            {"lo": (0, 0), "eth0": (1_001_024, 0), "wlan0": (200, 0)},
        )
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert plugin._active_interface == "eth0"
        assert _latest_status() == "online"

    async def test_explicit_interface_overrides_auto_detect(self, explicit_plugin, run_cycle):
        stdout = _two_snapshots(
            {"eth0": (0, 0), "wlan0": (9_999_999, 0)},
            {"eth0": (512, 0), "wlan0": (9_999_999, 0)},
        )
        run_cycle(explicit_plugin, lambda c: CmdResult(0, stdout, ""))
        assert explicit_plugin._active_interface == "eth0"
        assert _latest_metric("test-net-explicit", "rx_kbps") == pytest.approx(0.5)

    async def test_counter_reset_clamped_to_zero(self, plugin, run_cycle):
        stdout = _two_snapshots(
            {"eth0": (5000, 0)},
            {"eth0": (100, 0)},
        )
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_metric("test-net", "rx_kbps") == pytest.approx(0.0)

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(-1, "", "connection refused"))
        assert _latest_status() == "failed"

    async def test_malformed_output_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "garbage output", ""))
        assert _latest_status() == "failed"

    async def test_missing_interface_sets_failed(self, explicit_plugin, run_cycle):
        stdout = _two_snapshots({"wlan0": (0, 0)}, {"wlan0": (1024, 0)})
        run_cycle(explicit_plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status("test-net-explicit") == "failed"

    async def test_no_usable_interface_sets_failed(self, plugin, run_cycle):
        stdout = _two_snapshots({"lo": (0, 0), "veth0": (0, 0)}, {"lo": (0, 0), "veth0": (0, 0)})
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "failed"

    async def test_idle_interface_records_zero_rates(self, plugin, run_cycle):
        stdout = _two_snapshots({"eth0": (1000, 2000)}, {"eth0": (1000, 2000)})
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_metric("test-net", "rx_kbps") == pytest.approx(0.0)
        assert _latest_metric("test-net", "tx_kbps") == pytest.approx(0.0)


class TestNetworkUsageActions:
    async def test_on_action_always_returns_none(self, plugin):
        assert plugin.plan_action("anything") is None
