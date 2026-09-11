import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.btrfs import Btrfs, _sanitize_mount
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-btrfs", "id": "test-btrfs", "ssh_config": {"host": "test.host"}}

_GIB = 1024 ** 3
_COUNTERS = ('write_io_errs', 'read_io_errs', 'flush_io_errs',
             'corruption_errs', 'generation_errs')


def _probe(filesystems: dict) -> str:
    """The probe's FS/USE/DEV lines for {mountpoint: (size, used, unallocated, errors)}."""
    out = []
    for mount, (size, used, unallocated, errors) in filesystems.items():
        out.append(f"FS {mount}")
        out.append(f"USE size {size}")
        out.append(f"USE unallocated {unallocated}")
        out.append(f"USE used {used}")
        device = f"/dev/mapper/{mount.strip('/') or 'root'}"
        for counter in _COUNTERS:
            out.append(f"DEV [{device}].{counter}    {errors.get(counter, 0)}")
    return "\n".join(out) + "\n"


def _healthy(used_pct: float, size: int = 100 * _GIB, unallocated: int = 50 * _GIB) -> tuple:
    return (size, int(size * used_pct / 100), unallocated, {})


def _run(plugin, run_cycle, body, code=0, stderr=""):
    return run_cycle(plugin, lambda c: CmdResult(code, body, stderr))


def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == "test-btrfs"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == "test-btrfs") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Btrfs, CFG)


class TestSanitizeMount:
    def test_the_root_mount_is_named_root(self):
        assert _sanitize_mount("/") == "root"

    def test_separators_become_underscores(self):
        assert _sanitize_mount("/var/log") == "var_log"

    def test_case_is_kept_so_the_label_reads_back(self):
        assert _sanitize_mount("/Storage") == "Storage"


class TestCollection:
    async def test_all_healthy_is_ok(self, plugin, run_cycle):
        _run(plugin, run_cycle, _probe({"/": _healthy(10), "/Storage": _healthy(20)}))
        assert _latest_status() == "online"
        assert _latest_metric("filesystems_total") == 2
        assert _latest_metric("filesystems_ok") == 2
        assert _latest_metric("filesystems_degraded") == 0

    async def test_per_filesystem_usage_recorded(self, plugin, run_cycle):
        _run(plugin, run_cycle, _probe({"/": _healthy(10), "/Storage": _healthy(55)}))
        assert _latest_metric("fs_usage_root") == pytest.approx(10.0)
        assert _latest_metric("fs_usage_Storage") == pytest.approx(55.0)
        assert _latest_metric("btrfs_usage_max") == pytest.approx(55.0)

    async def test_usage_is_raw_so_a_dup_profile_reads_as_the_fuller_filesystem(
            self, plugin, run_cycle):
        # 1TB of data written DUP occupies 2TB of a 4TB device: half full, not a quarter.
        _run(plugin, run_cycle, _probe({"/Storage": (4000, 2000, 1000, {})}))
        assert _latest_metric("fs_usage_Storage") == pytest.approx(50.0)

    @pytest.mark.parametrize("counter", _COUNTERS)
    async def test_any_device_error_counter_fails(self, plugin, run_cycle, counter):
        size, used, unallocated, _ = _healthy(10)
        _run(plugin, run_cycle, _probe({"/": (size, used, unallocated, {counter: 3})}))
        assert _latest_status() == "failed", f"Expected failed for {counter}"
        assert _latest_metric("filesystems_degraded") == 1
        assert _latest_metric("fs_errors_root") == 3
        assert _latest_metric("device_errors") == 3

    async def test_errors_outrank_an_empty_filesystem(self, plugin, run_cycle):
        size, used, unallocated, _ = _healthy(1)
        _run(plugin, run_cycle, _probe({"/": (size, used, unallocated, {"read_io_errs": 1})}))
        assert _latest_status() == "failed"

    async def test_usage_over_warning_warns(self, plugin, run_cycle):
        _run(plugin, run_cycle, _probe({"/": _healthy(85)}))
        assert _latest_status() == "warning"

    async def test_usage_at_threshold_fails(self, plugin, run_cycle):
        _run(plugin, run_cycle, _probe({"/": _healthy(90)}))
        assert _latest_status() == "failed"

    async def test_custom_thresholds_apply(self, make_plugin, run_cycle):
        p = make_plugin(Btrfs, dict(CFG, warning=60, threshold=75))
        _run(p, run_cycle, _probe({"/": _healthy(80)}))
        assert _latest_status() == "failed"

    async def test_exhausted_unallocated_space_warns_on_an_empty_filesystem(
            self, plugin, run_cycle):
        _run(plugin, run_cycle, _probe({"/": _healthy(5, unallocated=0)}))
        assert _latest_status() == "warning"
        assert _latest_metric("fs_unallocated_root") == 0

    async def test_unallocated_warning_is_configurable(self, make_plugin, run_cycle):
        p = make_plugin(Btrfs, dict(CFG, unallocated_warning=10 * _GIB))
        _run(p, run_cycle, _probe({"/": _healthy(5, unallocated=5 * _GIB)}))
        assert _latest_status() == "warning"

    async def test_named_filesystems_narrow_the_report(self, make_plugin, run_cycle):
        p = make_plugin(Btrfs, dict(CFG, filesystems=["/Storage"]))
        _run(p, run_cycle, _probe({"/": _healthy(10), "/Storage": _healthy(20)}))
        assert _latest_metric("filesystems_total") == 1
        assert _latest_metric("fs_usage_Storage") == pytest.approx(20.0)

    async def test_no_filesystems_sets_offline(self, plugin, run_cycle):
        _run(plugin, run_cycle, "")
        assert _latest_status() == "offline"

    async def test_a_filesystem_with_no_size_is_skipped(self, plugin, run_cycle):
        _run(plugin, run_cycle, "FS /broken\nDEV [/dev/x].read_io_errs 0\n")
        assert _latest_status() == "offline"

    async def test_missing_btrfs_progs_fails(self, plugin, run_cycle):
        _run(plugin, run_cycle, "ERROR btrfs-progs not found\n", code=1)
        assert _latest_status() == "failed"

    async def test_transport_failure_sets_failed(self, plugin, run_cycle):
        _run(plugin, run_cycle, "", code=-1, stderr="timeout")
        assert _latest_status() == "failed"


class TestProbe:
    def test_the_probe_reads_through_mountpoints_so_it_needs_no_privilege(self, plugin):
        probe = plugin.commands()[0].text
        assert "sudo" not in probe
        assert "btrfs filesystem show" not in probe

    def test_one_command_per_cycle(self, plugin):
        assert len(plugin.commands()) == 1


class TestUiSpec:
    def test_per_filesystem_repeat_card_is_its_own_row(self, plugin):
        assert ['btrfs_filesystems'] in plugin.UI_SPEC['layout']

    def test_a_filesystem_with_errors_colors_failed_however_empty(self, plugin):
        assert plugin._item_level({'usage_pct': 1.0, 'errors': 2, 'unallocated': 50 * _GIB}) \
            == 'failed'

    def test_an_error_count_shows_beside_the_percentage(self, plugin):
        assert '2 dev err' in plugin._item_text({'usage_pct': 41.7, 'errors': 2})
