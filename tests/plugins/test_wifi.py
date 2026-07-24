import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.wifi import Wifi, _parse_wireless, _auto_detect_interface
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-wifi",
    "id":   "test-wifi",
    "ssh_config": {"host": "test.host"},
}

WIRELESS_HEADER = (
    "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
)


def _make_wireless(ifaces: dict) -> str:
    lines = [WIRELESS_HEADER]
    for iface, (link, level) in ifaces.items():
        lines.append(f" {iface}: 0000   {link}.  {level}.  -256        0      0      0      0      0        0\n")
    return "".join(lines)


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Wifi, BASE_CFG)


@pytest.fixture
def explicit_plugin(make_plugin):
    return make_plugin(Wifi, {
        "name": "test-wifi-explicit",
        "id": "test-wifi-explicit",
        "interface": "wlan0",
        "ssh_config": {"host": "test.host"},
    })


def _latest_status(plugin_id: str = "test-wifi"):
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


class TestParseWireless:
    def test_parses_link_and_level(self):
        block = _make_wireless({"wlan0": (65, -45)})
        result = _parse_wireless(block)
        assert result["wlan0"] == (65.0, -45.0)

    def test_strips_trailing_dot(self):
        block = _make_wireless({"wlan0": (70, -30)})
        assert _parse_wireless(block)["wlan0"] == (70.0, -30.0)

    def test_skips_header_rows(self):
        block = _make_wireless({"wlan0": (50, -50)})
        result = _parse_wireless(block)
        assert "Inter" not in result and "face" not in result

    def test_multiple_interfaces(self):
        block = _make_wireless({"wlan0": (65, -45), "wlan1": (20, -80)})
        result = _parse_wireless(block)
        assert set(result) == {"wlan0", "wlan1"}


class TestAutoDetect:
    def test_picks_strongest_link(self):
        stats = {"wlan0": (30.0, -70.0), "wlan1": (65.0, -45.0)}
        assert _auto_detect_interface(stats) == "wlan1"

    def test_none_when_empty(self):
        assert _auto_detect_interface({}) is None


class TestWifiCollection:
    async def test_strong_signal_online(self, plugin, run_cycle):
        stdout = _make_wireless({"wlan0": (65, -45)})
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "online"
        assert _latest_metric("test-wifi", "link_quality") == pytest.approx(65.0)
        assert _latest_metric("test-wifi", "signal_dbm") == pytest.approx(-45.0)

    async def test_weak_signal_warning(self, plugin, run_cycle):
        stdout = _make_wireless({"wlan0": (30, -75)})
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "warning"

    async def test_very_weak_signal_failed(self, plugin, run_cycle):
        stdout = _make_wireless({"wlan0": (15, -90)})
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "failed"

    async def test_auto_detects_strongest(self, plugin, run_cycle):
        stdout = _make_wireless({"wlan0": (25, -80), "wlan1": (68, -40)})
        result = run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert result.settings[f"wifi:{plugin.id}:active_interface"] == "wlan1"

    async def test_explicit_interface_missing_fails(self, explicit_plugin, run_cycle):
        stdout = _make_wireless({"wlan1": (60, -50)})
        run_cycle(explicit_plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status("test-wifi-explicit") == "failed"

    async def test_no_wireless_interface_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, WIRELESS_HEADER, ""))
        assert _latest_status() == "failed"

    async def test_ssh_failure_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(-1, "", "no route"))
        assert _latest_status() == "failed"


class TestWifiActions:
    async def test_on_action_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
