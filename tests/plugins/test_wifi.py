import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.wifi import Wifi
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-wifi", "id": "test-wifi", "ssh_config": {"host": "test.host"}}

WIRELESS_HEADER = (
    "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
)


def _make_wireless(ifaces: dict) -> str:
    lines = [WIRELESS_HEADER]
    for iface, (link, level) in ifaces.items():
        lines.append(f" {iface}: 0000   {link}.  {level}.  -256        0      0      0      0      0        0\n")
    return "".join(lines)



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-wifi"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-wifi") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Wifi, CFG)


from vigil.plugins.wifi import _parse_wireless, _strongest_interface


class TestParseWireless:
    def test_parses_link_and_level(self):
        assert _parse_wireless(_make_wireless({"wlan0": (65, -45)}))["wlan0"] == (65.0, -45.0)

    def test_skips_header_rows(self):
        result = _parse_wireless(_make_wireless({"wlan0": (50, -50)}))
        assert "Inter" not in result and "face" not in result

    def test_picks_strongest_link(self):
        assert _strongest_interface({"wlan0": (30.0, -70.0), "wlan1": (65.0, -45.0)}) == "wlan1"

    def test_none_when_empty(self):
        assert _strongest_interface({}) is None


class TestCollection:
    async def test_a_good_link_is_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_wireless({"wlan0": (65, -45)}), ""))
        assert _latest_status() == "online"
        assert _latest_metric("link_quality") == pytest.approx(65.0)

    async def test_weak_signal_warning(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_wireless({"wlan0": (30, -75)}), ""))
        assert _latest_status() == "warning"

    async def test_auto_detects_strongest(self, plugin, run_cycle):
        result = run_cycle(plugin, lambda c: CmdResult(
            0, _make_wireless({"wlan0": (25, -80), "wlan1": (68, -40)}), ""))
        assert result.settings[f"network:{plugin.id}:wifi_interface"] == "wlan1"

    async def test_explicit_interface_missing_fails(self, make_plugin, run_cycle):
        p = make_plugin(Wifi, dict(CFG, interface='wlan0'))
        run_cycle(p, lambda c: CmdResult(0, _make_wireless({"wlan1": (60, -50)}), ""))
        assert _latest_status() == "failed"

    async def test_no_wireless_interface_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, WIRELESS_HEADER, ""))
        assert _latest_status() == "failed"
