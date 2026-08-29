import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.throughput import Throughput
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-throughput", "id": "test-throughput", "ssh_config": {"host": "test.host"}}

NET_DEV_HEADER = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
)


def _make_net_dev(ifaces: dict) -> str:
    lines = [NET_DEV_HEADER]
    for iface, (rx, tx) in ifaces.items():
        lines.append(f"  {iface}: {rx} 100 0 0 0 0 0 0 {tx} 50 0 0 0 0 0 0\n")
    return "".join(lines)


def _two_snapshots(ifaces1: dict, ifaces2: dict) -> str:
    return _make_net_dev(ifaces1) + _make_net_dev(ifaces2)


def _default_snapshots() -> str:
    return _two_snapshots(
        {"lo": (0, 0), "eth0": (1_000_000, 500_000)},
        {"lo": (0, 0), "eth0": (1_001_024, 500_512)},
    )



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == "test-throughput"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == "test-throughput") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Throughput, CFG)


from vigil.plugins.throughput import _parse_net_dev, _busiest_interface, _format_rate


class TestParseNetDev:
    def test_parses_rx_and_tx(self):
        assert _parse_net_dev(_make_net_dev({"eth0": (1024, 512)}))["eth0"] == (1024, 512)

    def test_skips_header_lines(self):
        result = _parse_net_dev(_make_net_dev({"eth0": (0, 0)}))
        assert "Inter" not in result and "face" not in result

    def test_picks_highest_traffic_interface(self):
        assert _busiest_interface({"eth0": (1_000_000, 500_000), "wlan0": (100, 50)}) == "eth0"

    def test_excludes_virtual_prefixes(self):
        stats = {p: (9_999_999, 0) for p in
                 ("lo", "veth0", "docker0", "virbr0", "br-abc", "tun0", "tap0")}
        stats["eth0"] = (1000, 500)
        assert _busiest_interface(stats) == "eth0"

    def test_returns_none_when_no_candidates(self):
        assert _busiest_interface({"lo": (100, 100), "veth0": (200, 200)}) is None


class TestFormatRate:
    def test_below_1024_shows_kbps(self):
        assert _format_rate(512.0) == "512.0 KB/s"

    def test_at_1024_shows_mbps(self):
        assert _format_rate(1024.0) == "1.0 MB/s"


class TestCollection:
    async def test_rates_are_the_delta_between_snapshots(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _default_snapshots(), ""))
        assert _latest_status() == "online"
        assert _latest_metric("rx_kbps") == pytest.approx(1.0)

    async def test_active_interface_is_persisted(self, plugin, run_cycle):
        result = run_cycle(plugin, lambda c: CmdResult(0, _default_snapshots(), ""))
        assert result.settings[f"network:{plugin.id}:throughput_interface"] == "eth0"
        assert plugin.active_interface_text == "eth0"

    async def test_explicit_interface_overrides_auto_detect(self, make_plugin, run_cycle):
        p = make_plugin(Throughput, dict(CFG, interface='eth0'))
        run_cycle(p, lambda c: CmdResult(0, _two_snapshots(
            {"eth0": (0, 0), "wlan0": (9_999_999, 0)},
            {"eth0": (512, 0), "wlan0": (9_999_999, 0)},
        ), ""))
        assert _latest_metric("rx_kbps") == pytest.approx(0.5)

    async def test_missing_explicit_interface_fails(self, make_plugin, run_cycle):
        p = make_plugin(Throughput, dict(CFG, interface='eth0'))
        run_cycle(p, lambda c: CmdResult(
            0, _two_snapshots({"wlan0": (0, 0)}, {"wlan0": (1024, 0)}), ""))
        assert _latest_status() == "failed"

    async def test_counter_reset_clamped_to_zero(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(
            0, _two_snapshots({"eth0": (5000, 0)}, {"eth0": (100, 0)}), ""))
        assert _latest_metric("rx_kbps") == pytest.approx(0.0)

    async def test_malformed_output_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "garbage output", ""))
        assert _latest_status() == "failed"

    async def test_no_usable_interface_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _two_snapshots(
            {"lo": (0, 0), "veth0": (0, 0)}, {"lo": (0, 0), "veth0": (0, 0)}), ""))
        assert _latest_status() == "failed"
