import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.disks import (
    Disks, _module_options, _worst, _sanitize_pool, _parse_diskstats,
    _is_physical, _auto_detect_device, _format_rate,
)
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-disks",
    "id":   "test-disks",
    "ssh_config": {"host": "test.host"},
}

ALL_MODULES = ['smart', 'zfs', 'io']


def _make_diskstats(devices: dict) -> str:
    lines = []
    for i, (name, (rd, wr)) in enumerate(devices.items()):
        lines.append(f"   8       {i} {name} 100 0 {rd} 50 200 0 {wr} 80 0 0 0 0")
    return "\n".join(lines) + "\n"


def _two_snaps(d1: dict, d2: dict) -> str:
    return _make_diskstats(d1) + "---SNAP---\n" + _make_diskstats(d2)


def _make_zpool(pools: dict) -> str:
    return "".join(f"{name}\t{health}\t{capacity}%\n"
                   for name, (health, capacity) in pools.items())


def _outputs(plugin, *, smart=None, zpool=None, diskstats=None, codes=None, stderrs=None):
    """Map the plugin's concatenated commands to fake results, in the order
    the enabled modules declared them."""
    smart = smart if smart is not None else "PASS /dev/sda\nPASS /dev/sdb\n"
    zpool = zpool if zpool is not None else _make_zpool({"tank": ("ONLINE", 40)})
    diskstats = diskstats if diskstats is not None else _two_snaps(
        {"sda": (0, 0), "sdb": (0, 0)}, {"sda": (2, 4), "sdb": (0, 0)})

    bodies = []
    for command in plugin.commands():
        if 'smartctl' in command.text:
            bodies.append(smart)
        elif 'zpool' in command.text:
            bodies.append(zpool)
        else:
            bodies.append(diskstats)
    codes = codes or [0] * len(bodies)
    stderrs = stderrs or [""] * len(bodies)
    return [CmdResult(code, body, err)
            for code, body, err in zip(codes, bodies, stderrs)]


def _collect(plugin, run_cycle, **kwargs):
    results = iter(_outputs(plugin, **kwargs))
    return run_cycle(plugin, lambda c: next(results))


def _latest_status(plugin_id: str = "test-disks"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-disks"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Disks, dict(BASE_CFG, modules=ALL_MODULES))


class TestModuleSelection:
    def test_known_modules_are_in_canonical_order(self, plugin):
        assert [m.key for m in plugin.modules] == ['smart', 'zfs', 'io']

    def test_every_module_is_opt_in(self, make_plugin):
        p = make_plugin(Disks, dict(BASE_CFG, modules=['io', 'smart']))
        assert [m.key for m in p.modules] == ['smart', 'io']
        assert len(p.commands()) == 2

    def test_an_absent_modules_block_enables_the_defaults(self, make_plugin):
        assert [m.key for m in make_plugin(Disks, BASE_CFG).modules] == ['io']

    def test_an_empty_modules_block_enables_nothing(self, make_plugin):
        assert make_plugin(Disks, dict(BASE_CFG, modules=[])).modules == []

    def test_mapping_form_disables_module(self, make_plugin):
        p = make_plugin(Disks, dict(BASE_CFG, modules={
            'zfs': {'warning': 50, 'threshold': 60},
            'smart': False,
            'io': {'enabled': False},
        }))
        assert [m.key for m in p.modules] == ['zfs']
        assert (p.modules[0].warning, p.modules[0].threshold) == (50, 60)

    def test_bare_true_keeps_defaults(self, make_plugin):
        p = make_plugin(Disks, dict(BASE_CFG, modules={'zfs': True}))
        assert (p.modules[0].warning, p.modules[0].threshold) == (80, 90)

    def test_unknown_module_rejected(self, make_plugin):
        with pytest.raises(ValueError, match="unknown module"):
            make_plugin(Disks, dict(BASE_CFG, modules=['raid']))

    def test_bad_modules_type_rejected(self, make_plugin):
        with pytest.raises(ValueError, match="must be a mapping or a list"):
            make_plugin(Disks, dict(BASE_CFG, modules="smart"))

    def test_options_resolve_to_canonical_order(self):
        assert list(_module_options({'modules': ['io', 'smart']})) == ['smart', 'io']


class TestWorst:
    def test_offline_ranks_below_warning(self):
        assert _worst(['offline', 'warning']) == 'warning'

    def test_failed_wins(self):
        assert _worst(['online', 'warning', 'failed']) == 'failed'

    def test_empty_is_online(self):
        assert _worst([]) == 'online'


class TestParseDiskstats:
    def test_parses_read_write_sectors(self):
        assert _parse_diskstats(_make_diskstats({"sda": (1000, 2000)}))["sda"] == (1000, 2000)

    def test_multiple_devices(self):
        result = _parse_diskstats(_make_diskstats({"sda": (1, 2), "nvme0n1": (3, 4)}))
        assert set(result) == {"sda", "nvme0n1"}


class TestIsPhysical:
    def test_whole_disks_are_physical(self):
        assert _is_physical("sda")
        assert _is_physical("nvme0n1")
        assert _is_physical("mmcblk0")

    def test_partitions_not_physical(self):
        assert not _is_physical("sda1")
        assert not _is_physical("nvme0n1p1")
        assert not _is_physical("mmcblk0p2")

    def test_virtual_not_physical(self):
        assert not _is_physical("loop0")
        assert not _is_physical("ram0")
        assert not _is_physical("dm-0")


class TestAutoDetect:
    def test_picks_busiest_disk(self):
        assert _auto_detect_device({"sda": (0, 0), "sdb": (0, 0)},
                                   {"sda": (100, 0), "sdb": (5000, 0)}) == "sdb"

    def test_excludes_partitions(self):
        assert _auto_detect_device({"sda1": (0, 0), "sda": (0, 0)},
                                   {"sda1": (9999, 0), "sda": (10, 0)}) == "sda"

    def test_returns_none_when_no_candidates(self):
        assert _auto_detect_device({}, {}) is None


class TestFormatRate:
    def test_below_1024_shows_kbps(self):
        assert _format_rate(512.0) == "512.0 KB/s"

    def test_at_1024_shows_mbps(self):
        assert _format_rate(2048.0) == "2.0 MB/s"


class TestSanitizePool:
    def test_lowercases_and_replaces_separators(self):
        assert _sanitize_pool("Tank-Fast.01") == "tank_fast_01"


class TestCollection:
    async def test_all_modules_record_their_metrics(self, plugin, run_cycle):
        _collect(plugin, run_cycle)
        assert _latest_metric("disks_total") == 2
        assert _latest_metric("disks_ok") == 2
        assert _latest_metric("pools_total") == 1
        assert _latest_metric("pool_usage_tank") == pytest.approx(40.0)
        assert _latest_metric("read_kbps") == pytest.approx(1.0)
        assert _latest_metric("write_kbps") == pytest.approx(2.0)
        assert _latest_status() == "online"

    async def test_no_modules_reports_offline(self, make_plugin, run_cycle):
        run_cycle(make_plugin(Disks, dict(BASE_CFG, modules=[])))
        assert _latest_status() == "offline"

    async def test_active_device_persisted(self, plugin, run_cycle):
        result = _collect(plugin, run_cycle)
        assert result.settings[f"disks:{plugin.id}:active_device"] == "sda"
        assert plugin._io_device == "sda"

    async def test_device_text_falls_back_when_module_off(self, make_plugin):
        assert make_plugin(Disks, dict(BASE_CFG, modules=['smart']))._io_device == "--"

    async def test_worst_module_status_wins(self, plugin, run_cycle):
        _collect(plugin, run_cycle, zpool=_make_zpool({"tank": ("DEGRADED", 10)}))
        assert _latest_status() == "failed"

    async def test_one_failed_command_does_not_stop_the_others(self, plugin, run_cycle):
        _collect(plugin, run_cycle, codes=[0, 0, -1], stderrs=["", "", "no device"])
        assert _latest_metric("disks_ok") == 2
        assert _latest_metric("pools_total") == 1
        assert _latest_status() == "failed"

    async def test_on_action_returns_none(self, plugin):
        assert plugin.plan_action("anything") is None


class TestSmartModule:
    @pytest.fixture
    def smart(self, make_plugin):
        return make_plugin(Disks, dict(BASE_CFG, modules=['smart']))

    async def test_all_pass_is_online(self, smart, run_cycle):
        _collect(smart, run_cycle, smart="PASS /dev/sda\nPASS /dev/sdb")
        assert _latest_status() == "online"

    async def test_one_fail_sets_failed(self, smart, run_cycle):
        _collect(smart, run_cycle, smart="PASS /dev/sda\nFAIL /dev/sdb\nFAIL /dev/sdc")
        assert _latest_status() == "failed"
        assert _latest_metric("disks_total") == 3
        assert _latest_metric("disks_ok") == 1
        assert _latest_metric("disks_failed") == 2

    async def test_no_disks_sets_offline(self, smart, run_cycle):
        _collect(smart, run_cycle, smart="")
        assert _latest_status() == "offline"

    async def test_malformed_lines_skipped(self, smart, run_cycle):
        _collect(smart, run_cycle, smart="PASS /dev/sda\nsome random noise\nFAIL /dev/sdb")
        assert _latest_metric("disks_total") == 2

    async def test_ssh_failure_sets_failed(self, smart, run_cycle):
        _collect(smart, run_cycle, codes=[-1], stderrs=["SSH timeout"])
        assert _latest_status() == "failed"


class TestBlindChecksAreNotHealthy:
    """The failure that matters most: when smartctl cannot run, the monitor
    must not report healthy disks. Found in production, where a missing sudo
    right made a host report 4 OK disks while reading none of them."""

    @pytest.fixture
    def smart(self, make_plugin):
        p = make_plugin(Disks, dict(BASE_CFG, modules=['smart']))
        p.commands()
        return p

    def test_a_privilege_error_is_not_a_passing_disk(self, smart):
        result = smart.parse([CmdResult(
            0, "UNKNOWN /dev/sda sudo: must be owned by uid 0 and have the setuid bit set", "")])

        assert result.status == 'failed'
        assert result.metrics['disks_ok'] == 0
        assert result.metrics['disks_unknown'] == 1
        assert any('Could not read SMART health' in m for m, _ in result.logs)

    def test_a_healthy_disk_still_passes(self, smart):
        result = smart.parse([CmdResult(0, "PASS /dev/sda\nPASS /dev/sdb", "")])

        assert result.status == 'online'
        assert result.metrics['disks_ok'] == 2
        assert result.metrics['disks_unknown'] == 0

    def test_one_unreadable_disk_fails_the_monitor(self, smart):
        result = smart.parse([CmdResult(0, "PASS /dev/sda\nUNKNOWN /dev/sdb Permission denied", "")])

        assert result.status == 'failed'
        assert (result.metrics['disks_ok'], result.metrics['disks_unknown']) == (1, 1)

    def test_the_script_classifies_by_positive_assertion(self):
        from vigil.plugins.disks import _SMART_SCRIPT
        assert "test result: *PASSED" in _SMART_SCRIPT
        assert "UNKNOWN $d" in _SMART_SCRIPT


class TestNonPhysicalDevices:
    """zram, ZFS zvols and loop/md/dm nodes are reported as type "disk" by
    lsblk but have no SMART. Probing them turned a healthy host permanently
    red once unreadable disks stopped counting as healthy."""

    @pytest.fixture
    def smart(self, make_plugin):
        p = make_plugin(Disks, dict(BASE_CFG, modules=['smart']))
        p.commands()
        return p

    def test_virtual_devices_are_filtered_before_probing(self):
        from vigil.plugins.disks import _SMART_SCRIPT
        assert "grep -Ev '^(zram|zd|loop|md|dm-|sr|fd|ram)'" in _SMART_SCRIPT

    def test_an_unsupported_device_is_skipped_not_counted(self, smart):
        result = smart.parse([CmdResult(0, "PASS /dev/sda\nSKIP /dev/zd0", "")])

        assert result.status == 'online'
        assert result.metrics['disks_total'] == 1
        assert result.metrics['disks_unknown'] == 0

    def test_skips_alone_look_like_no_disks(self, smart):
        assert smart.parse([CmdResult(0, "SKIP /dev/zram0\nSKIP /dev/zd0", "")]).status == 'offline'


class TestZfsModule:
    @pytest.fixture
    def zfs(self, make_plugin):
        return make_plugin(Disks, dict(BASE_CFG, modules=['zfs']))

    async def test_all_online_is_ok(self, zfs, run_cycle):
        _collect(zfs, run_cycle, zpool=_make_zpool(
            {"pool1": ("ONLINE", 10), "pool2": ("ONLINE", 20)}))
        assert _latest_status() == "online"
        assert _latest_metric("pools_total") == 2
        assert _latest_metric("pools_ok") == 2
        assert _latest_metric("pools_degraded") == 0

    async def test_per_pool_usage_recorded(self, zfs, run_cycle):
        _collect(zfs, run_cycle, zpool=_make_zpool(
            {"tank": ("ONLINE", 10), "back-up": ("ONLINE", 55)}))
        assert _latest_metric("pool_usage_tank") == pytest.approx(10.0)
        assert _latest_metric("pool_usage_back_up") == pytest.approx(55.0)
        assert _latest_metric("zfs_usage_max") == pytest.approx(55.0)

    @pytest.mark.parametrize("bad_state", ["DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"])
    async def test_all_unhealthy_states_trigger_failed(self, zfs, run_cycle, bad_state):
        _collect(zfs, run_cycle, zpool=_make_zpool({"pool1": (bad_state, 10)}))
        assert _latest_status() == "failed", f"Expected failed for state {bad_state}"
        assert _latest_metric("pools_degraded") == 1

    async def test_usage_over_warning_warns(self, zfs, run_cycle):
        _collect(zfs, run_cycle, zpool=_make_zpool({"tank": ("ONLINE", 85)}))
        assert _latest_status() == "warning"

    async def test_usage_at_threshold_fails(self, zfs, run_cycle):
        _collect(zfs, run_cycle, zpool=_make_zpool({"tank": ("ONLINE", 90)}))
        assert _latest_status() == "failed"

    async def test_custom_thresholds_apply(self, make_plugin, run_cycle):
        p = make_plugin(Disks, dict(BASE_CFG, id="disks-zfs-75", name="disks-zfs-75",
                                    modules={'zfs': {'warning': 60, 'threshold': 75}}))
        _collect(p, run_cycle, zpool=_make_zpool({"tank": ("ONLINE", 80)}))
        assert _latest_status("disks-zfs-75") == "failed"

    async def test_named_pools_narrow_the_query(self, make_plugin):
        p = make_plugin(Disks, dict(BASE_CFG, modules={'zfs': {'pools': ['tank', 'backup']}}))
        assert "name,health,capacity tank backup" in p.commands()[0].text

    async def test_no_pools_sets_offline(self, zfs, run_cycle):
        _collect(zfs, run_cycle, zpool="")
        assert _latest_status() == "offline"

    async def test_malformed_lines_skipped(self, zfs, run_cycle):
        _collect(zfs, run_cycle,
                 zpool="pool1\tONLINE\t10%\njust_one_word\npool2\tONLINE\t20%\n")
        assert _latest_metric("pools_total") == 2

    async def test_ssh_failure_sets_failed(self, zfs, run_cycle):
        _collect(zfs, run_cycle, zpool="", codes=[-1], stderrs=["timeout"])
        assert _latest_status() == "failed"


class TestIoModule:
    @pytest.fixture
    def io(self, make_plugin):
        return make_plugin(Disks, dict(BASE_CFG, modules=['io']))

    async def test_throughput_recorded(self, io, run_cycle):
        _collect(io, run_cycle, diskstats=_two_snaps({"sda": (0, 0)}, {"sda": (2, 4)}))
        assert _latest_status() == "online"
        assert _latest_metric("read_kbps") == pytest.approx(1.0)
        assert _latest_metric("write_kbps") == pytest.approx(2.0)

    async def test_counter_reset_clamped(self, io, run_cycle):
        _collect(io, run_cycle, diskstats=_two_snaps({"sda": (5000, 5000)}, {"sda": (10, 10)}))
        assert _latest_metric("read_kbps") == pytest.approx(0.0)

    async def test_auto_detects_busiest_device(self, io, run_cycle):
        result = _collect(io, run_cycle, diskstats=_two_snaps(
            {"sda": (0, 0), "sdb": (0, 0)}, {"sda": (2, 0), "sdb": (1000, 0)}))
        assert result.settings[f"disks:{io.id}:active_device"] == "sdb"

    async def test_explicit_device_missing_fails(self, make_plugin, run_cycle):
        p = make_plugin(Disks, dict(BASE_CFG, id="disks-io-x", name="disks-io-x",
                                    modules={'io': {'device': 'sda'}}))
        _collect(p, run_cycle, diskstats=_two_snaps({"sdb": (0, 0)}, {"sdb": (2, 0)}))
        assert _latest_status("disks-io-x") == "failed"

    async def test_malformed_fails(self, io, run_cycle):
        _collect(io, run_cycle, diskstats="no separator here")
        assert _latest_status() == "failed"

    async def test_ssh_failure_fails(self, io, run_cycle):
        _collect(io, run_cycle, codes=[-1], stderrs=["err"])
        assert _latest_status() == "failed"


class TestUiSpec:
    def test_layout_covers_enabled_modules_only(self, make_plugin):
        spec = make_plugin(Disks, dict(BASE_CFG, modules=['smart'])).UI_SPEC
        assert spec['layout'][0] == ['host_card', 'smart_total_card', 'smart_ok_card',
                                     'smart_failed_card']
        assert 'read_chart' not in spec['charts'] and 'zfs_chart' not in spec['charts']

    def test_all_modules_contribute_cards_and_charts(self, plugin):
        spec = plugin.UI_SPEC
        assert {'smart_total_card', 'zfs_total_card', 'zfs_usage_card', 'zfs_pools',
                'io_device_card', 'read_card', 'write_card'} <= set(spec['cards'])
        assert {'zfs_chart', 'read_chart', 'write_chart'} <= set(spec['charts'])

    def test_per_pool_repeat_card_is_its_own_row(self, plugin):
        assert ['zfs_pools'] in plugin.UI_SPEC['layout']
