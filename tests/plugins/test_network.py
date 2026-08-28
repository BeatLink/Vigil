import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.network import (
    Network, _module_options, _worst, _parse_net_dev, _busiest_interface,
    _format_rate, _parse_states, _parse_wireless, _strongest_interface,
)
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-network",
    "id":   "test-network",
    "ssh_config": {"host": "test.host"},
}

ALL_MODULES = ['throughput', 'connections', 'wifi']

NET_DEV_HEADER = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
)

TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"

WIRELESS_HEADER = (
    "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
)


def _make_net_dev(ifaces: dict) -> str:
    lines = [NET_DEV_HEADER]
    for iface, (rx, tx) in ifaces.items():
        lines.append(f"  {iface}: {rx} 100 0 0 0 0 0 0 {tx} 50 0 0 0 0 0 0\n")
    return "".join(lines)


def _two_snapshots(ifaces1: dict, ifaces2: dict) -> str:
    return _make_net_dev(ifaces1) + _make_net_dev(ifaces2)


def _make_tcp(states: list) -> str:
    lines = [TCP_HEADER]
    for i, st in enumerate(states):
        lines.append(f"   {i}: 0100007F:0050 00000000:0000 {st} 00000000:00000000 00:00000000 00000000     0        0 0\n")
    return "".join(lines)


def _make_wireless(ifaces: dict) -> str:
    lines = [WIRELESS_HEADER]
    for iface, (link, level) in ifaces.items():
        lines.append(f" {iface}: 0000   {link}.  {level}.  -256        0      0      0      0      0        0\n")
    return "".join(lines)


def _outputs(plugin, *, net_dev=None, tcp_states=("01", "0A", "06"), wireless=None,
             codes=None, stderrs=None):
    """Map the plugin's concatenated commands to fake results, in the order
    the enabled modules declared them."""
    net_dev = net_dev if net_dev is not None else _two_snapshots(
        {"lo": (0, 0), "eth0": (1_000_000, 500_000)},
        {"lo": (0, 0), "eth0": (1_001_024, 500_512)},
    )
    wireless = wireless if wireless is not None else _make_wireless({"wlan0": (65, -45)})

    bodies = []
    for command in plugin.commands():
        if '/proc/net/dev' in command.text:
            bodies.append(net_dev)
        elif '/proc/net/tcp' in command.text:
            bodies.append(_make_tcp(list(tcp_states)))
        else:
            bodies.append(wireless)
    codes = codes or [0] * len(bodies)
    stderrs = stderrs or [""] * len(bodies)
    return [CmdResult(code, body, err)
            for code, body, err in zip(codes, bodies, stderrs)]


def _collect(plugin, run_cycle, **kwargs):
    results = iter(_outputs(plugin, **kwargs))
    return run_cycle(plugin, lambda c: next(results))


def _latest_status(plugin_id: str = "test-network"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-network"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Network, dict(BASE_CFG, modules=ALL_MODULES))


class TestModuleSelection:
    def test_known_modules_are_in_canonical_order(self, plugin):
        assert [m.key for m in plugin.modules] == ['throughput', 'connections', 'wifi']

    def test_every_module_is_opt_in(self, make_plugin):
        p = make_plugin(Network, dict(BASE_CFG, modules=['wifi', 'throughput']))
        assert [m.key for m in p.modules] == ['throughput', 'wifi']
        assert len(p.commands()) == 2

    def test_an_absent_modules_block_enables_nothing(self, make_plugin):
        assert make_plugin(Network, BASE_CFG).modules == []

    def test_mapping_form_disables_module(self, make_plugin):
        p = make_plugin(Network, dict(BASE_CFG, modules={
            'connections': {'warning': 5, 'threshold': 10},
            'wifi': False,
            'throughput': {'enabled': False},
        }))
        assert [m.key for m in p.modules] == ['connections']
        assert p.modules[0].warning == 5

    def test_bare_true_keeps_defaults(self, make_plugin):
        p = make_plugin(Network, dict(BASE_CFG, modules={'connections': True}))
        assert p.modules[0].warning == 500

    def test_unknown_module_rejected(self, make_plugin):
        with pytest.raises(ValueError, match="unknown module"):
            make_plugin(Network, dict(BASE_CFG, modules=['bandwidth']))

    def test_bad_modules_type_rejected(self, make_plugin):
        with pytest.raises(ValueError, match="must be a mapping or a list"):
            make_plugin(Network, dict(BASE_CFG, modules="wifi"))

    def test_options_resolve_to_canonical_order(self):
        assert list(_module_options({'modules': ['wifi', 'connections']})) == [
            'connections', 'wifi']


class TestWorst:
    def test_offline_ranks_below_warning(self):
        assert _worst(['offline', 'warning']) == 'warning'

    def test_failed_wins(self):
        assert _worst(['online', 'warning', 'failed']) == 'failed'

    def test_empty_is_online(self):
        assert _worst([]) == 'online'


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


class TestParseStates:
    def test_counts_established_and_listen(self):
        counts = _parse_states(_make_tcp(["01", "01", "0A"]))
        assert counts["ESTABLISHED"] == 2 and counts["LISTEN"] == 1

    def test_ignores_header(self):
        assert sum(_parse_states(_make_tcp(["01"])).values()) == 1

    def test_handles_lowercase_hex(self):
        assert _parse_states(_make_tcp(["0a"]))["LISTEN"] == 1

    def test_unknown_state_ignored(self):
        assert sum(_parse_states(_make_tcp(["FF"])).values()) == 0


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
    async def test_all_modules_record_their_metrics(self, plugin, run_cycle):
        _collect(plugin, run_cycle)
        assert _latest_metric("rx_kbps") == pytest.approx(1.0)
        assert _latest_metric("tx_kbps") == pytest.approx(0.5)
        assert _latest_metric("conn_total") == pytest.approx(3.0)
        assert _latest_metric("conn_established") == pytest.approx(1.0)
        assert _latest_metric("conn_listen") == pytest.approx(1.0)
        assert _latest_metric("conn_time_wait") == pytest.approx(1.0)
        assert _latest_metric("link_quality") == pytest.approx(65.0)
        assert _latest_metric("signal_dbm") == pytest.approx(-45.0)
        assert _latest_status() == "online"

    async def test_no_modules_reports_offline(self, make_plugin, run_cycle):
        p = make_plugin(Network, BASE_CFG)
        run_cycle(p)
        assert _latest_status() == "offline"

    async def test_each_module_logs_a_line(self, plugin, run_cycle):
        result = _collect(plugin, run_cycle)
        assert len(result.logs) == 3

    async def test_active_interfaces_persisted(self, plugin, run_cycle):
        result = _collect(plugin, run_cycle)
        assert result.settings[f"network:{plugin.id}:throughput_interface"] == "eth0"
        assert result.settings[f"network:{plugin.id}:wifi_interface"] == "wlan0"
        assert plugin._throughput_interface == "eth0"
        assert plugin._wifi_interface == "wlan0"

    async def test_interface_text_falls_back_when_module_off(self, make_plugin):
        p = make_plugin(Network, dict(BASE_CFG, modules=['connections']))
        assert p._throughput_interface == "--"
        assert p._wifi_interface == "--"

    async def test_worst_module_status_wins(self, plugin, run_cycle):
        _collect(plugin, run_cycle, wireless=_make_wireless({"wlan0": (15, -90)}))
        assert _latest_status() == "failed"

    async def test_one_failed_command_does_not_stop_the_others(self, plugin, run_cycle):
        _collect(plugin, run_cycle, codes=[0, 0, -1], stderrs=["", "", "no device"])
        assert _latest_metric("rx_kbps") == pytest.approx(1.0)
        assert _latest_metric("conn_total") == pytest.approx(3.0)
        assert _latest_status() == "failed"


class TestThroughputModule:
    async def test_explicit_interface_overrides_auto_detect(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules={'throughput': {'interface': 'eth0'}}))
        _collect(p, run_cycle, net_dev=_two_snapshots(
            {"eth0": (0, 0), "wlan0": (9_999_999, 0)},
            {"eth0": (512, 0), "wlan0": (9_999_999, 0)},
        ))
        assert _latest_metric("rx_kbps") == pytest.approx(0.5)

    async def test_missing_explicit_interface_fails(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules={'throughput': {'interface': 'eth0'}}))
        _collect(p, run_cycle, net_dev=_two_snapshots({"wlan0": (0, 0)}, {"wlan0": (1024, 0)}))
        assert _latest_status() == "failed"

    async def test_counter_reset_clamped_to_zero(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules=['throughput']))
        _collect(p, run_cycle, net_dev=_two_snapshots({"eth0": (5000, 0)}, {"eth0": (100, 0)}))
        assert _latest_metric("rx_kbps") == pytest.approx(0.0)

    async def test_malformed_output_fails(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules=['throughput']))
        _collect(p, run_cycle, net_dev="garbage output")
        assert _latest_status() == "failed"

    async def test_no_usable_interface_fails(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules=['throughput']))
        _collect(p, run_cycle, net_dev=_two_snapshots(
            {"lo": (0, 0), "veth0": (0, 0)}, {"lo": (0, 0), "veth0": (0, 0)}))
        assert _latest_status() == "failed"


class TestConnectionsModule:
    async def test_warning_on_high_total(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules={
            'connections': {'warning': 2, 'threshold': 10}}))
        _collect(p, run_cycle, tcp_states=["01", "01", "01"])
        assert _latest_status() == "warning"

    async def test_failed_on_flood(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules={
            'connections': {'warning': 2, 'threshold': 3}}))
        _collect(p, run_cycle, tcp_states=["01", "01", "01", "01"])
        assert _latest_status() == "failed"

    async def test_zero_connections_records_zero(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules=['connections']))
        _collect(p, run_cycle, tcp_states=[])
        assert _latest_metric("conn_total") == pytest.approx(0.0)
        assert _latest_status() == "online"


class TestWifiModule:
    async def test_weak_signal_warning(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules=['wifi']))
        _collect(p, run_cycle, wireless=_make_wireless({"wlan0": (30, -75)}))
        assert _latest_status() == "warning"

    async def test_auto_detects_strongest(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules=['wifi']))
        result = _collect(p, run_cycle,
                          wireless=_make_wireless({"wlan0": (25, -80), "wlan1": (68, -40)}))
        assert result.settings[f"network:{p.id}:wifi_interface"] == "wlan1"

    async def test_explicit_interface_missing_fails(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules={'wifi': {'interface': 'wlan0'}}))
        _collect(p, run_cycle, wireless=_make_wireless({"wlan1": (60, -50)}))
        assert _latest_status() == "failed"

    async def test_no_wireless_interface_fails(self, make_plugin, run_cycle):
        p = make_plugin(Network, dict(BASE_CFG, modules=['wifi']))
        _collect(p, run_cycle, wireless=WIRELESS_HEADER)
        assert _latest_status() == "failed"


class TestUiSpec:
    def test_layout_covers_enabled_modules_only(self, make_plugin):
        p = make_plugin(Network, dict(BASE_CFG, modules=['connections']))
        spec = p.UI_SPEC
        assert spec['layout'][0] == ['host_card', 'conn_total_card', 'conn_established_card',
                                     'conn_listen_card', 'conn_timewait_card']
        assert 'rx_chart' not in spec['charts'] and 'quality_chart' not in spec['charts']

    def test_all_modules_contribute_cards_and_charts(self, plugin):
        spec = plugin.UI_SPEC
        assert {'iface_card', 'rx_card', 'tx_card', 'conn_total_card',
                'wifi_iface_card', 'quality_card', 'signal_card'} <= set(spec['cards'])
        assert {'rx_chart', 'tx_chart', 'conn_total_chart',
                'quality_chart', 'signal_chart'} <= set(spec['charts'])


class TestNetworkActions:
    async def test_on_action_returns_none(self, plugin):
        assert plugin.plan_action("anything") is None
