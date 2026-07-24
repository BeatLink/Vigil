import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.filesystems import (
    FilesystemsCollectorPlugin, _sanitize, _parse_inodes, _parse_readonly, _SNAP,
)
from vigil.collector.orchestration.types import CmdResult
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {"name": "test-fs", "id": "test-fs", "warning": 80, "threshold": 90,
            "ssh_config": {"host": "test.host"}}


def _latest_status(pid="test-fs"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric, name="test-fs"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


_HEADER = "Mounted on           1B-blocks        Used Use%"


_IHEADER = "Mounted on            Inodes IUsed IFree IUse%"


def _df(*rows):
    lines = [_HEADER]
    for mount, size, used, pct in rows:
        lines.append(f"{mount} {size} {used} {pct}%")
    return "\n".join(lines) + "\n"


def _df_inodes(*rows):
    lines = [_IHEADER]
    for mount, pct in rows:
        lines.append(f"{mount} {pct}" if pct == '-' else f"{mount} {pct}%")
    return "\n".join(lines) + "\n"


def _mounts(*rows):
    lines = []
    for mount, mode in rows:
        escaped = mount.replace('\\', '\\134').replace(' ', '\\040')
        lines.append(f"/dev/sda1 {escaped} ext4 {mode},relatime 0 0")
    return "\n".join(lines) + "\n"


def _combined(space, inodes=None, mounts=None):
    parts = [space]
    if inodes is not None:
        parts.append(inodes)
    if mounts is not None:
        parts.append(mounts)
    return f"\n{_SNAP}\n".join(parts)


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(FilesystemsCollectorPlugin, BASE_CFG)


class TestSanitize:
    def test_root(self):
        assert _sanitize('/') == 'root'

    def test_nested(self):
        assert _sanitize('/var/log') == 'var_log'


class TestFilesystemsCollection:
    async def test_all_healthy_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _df(
            ("/", 100_000_000_000, 40_000_000_000, 40),
            ("/home", 500_000_000_000, 100_000_000_000, 20),
        ), ""))
        assert _latest_status() == "online"
        assert _latest_metric("worst_used_pct") == pytest.approx(40.0)
        assert _latest_metric("fs_root_used_pct") == pytest.approx(40.0)
        assert _latest_metric("fs_home_used_pct") == pytest.approx(20.0)

    async def test_over_warning(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _df(
            ("/", 100, 85, 85),
        ), ""))
        assert _latest_status() == "warning"

    async def test_over_threshold_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _df(
            ("/", 100, 40, 40),
            ("/data", 100, 95, 95),
        ), ""))
        assert _latest_status() == "failed"
        assert _latest_metric("worst_used_pct") == pytest.approx(95.0)

    async def test_mount_with_space(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _df(
            ("/mnt/my drive", 100, 10, 10),
        ), ""))
        assert _latest_status() == "online"
        assert _latest_metric("fs_mnt_my_drive_used_pct") == pytest.approx(10.0)

    async def test_no_filesystems_offline(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _HEADER + "\n", ""))
        assert _latest_status() == "offline"

    async def test_df_failure_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "df: error"))
        assert _latest_status() == "failed"


class TestParseInodes:
    def test_basic(self):
        assert _parse_inodes(_df_inodes(("/", 12), ("/home", 90))) == {
            "/": 12.0, "/home": 90.0,
        }

    def test_omits_inodeless_filesystems(self):
        assert _parse_inodes(_df_inodes(("/tank", "-"), ("/", 5))) == {"/": 5.0}

    def test_mount_with_space(self):
        assert _parse_inodes(_df_inodes(("/mnt/my drive", 30))) == {"/mnt/my drive": 30.0}


class TestParseReadonly:
    def test_ro_and_rw(self):
        assert _parse_readonly(_mounts(("/", "rw"), ("/data", "ro"))) == {
            "/": False, "/data": True,
        }

    def test_decodes_escaped_space(self):
        assert _parse_readonly(_mounts(("/mnt/my drive", "ro"))) == {"/mnt/my drive": True}

    def test_ignores_malformed_lines(self):
        assert _parse_readonly("garbage\n/dev/sda1 /mnt ext4 rw 0 0\n") == {"/mnt": False}


class TestInodeExhaustion:
    async def test_healthy_inodes_stay_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _combined(
            _df(("/", 100, 10, 10)),
            _df_inodes(("/", 12)),
            _mounts(("/", "rw")),
        ), ""))
        assert _latest_status() == "online"
        assert _latest_metric("fs_root_inodes_pct") == pytest.approx(12.0)

    async def test_inode_exhaustion_fails_despite_free_space(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _combined(
            _df(("/", 100, 10, 10)),
            _df_inodes(("/", 97)),
            _mounts(("/", "rw")),
        ), ""))
        assert _latest_status() == "failed"
        assert _latest_metric("worst_used_pct") == pytest.approx(10.0)
        assert _latest_metric("worst_inodes_pct") == pytest.approx(97.0)

    async def test_inode_warning(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _combined(
            _df(("/", 100, 10, 10)),
            _df_inodes(("/", 88)),
            _mounts(("/", "rw")),
        ), ""))
        assert _latest_status() == "warning"

    async def test_inodeless_filesystem_records_no_metric(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _combined(
            _df(("/tank", 100, 10, 10)),
            _df_inodes(("/tank", "-")),
            _mounts(("/tank", "rw")),
        ), ""))
        assert _latest_status() == "online"
        assert _latest_metric("fs_tank_inodes_pct") is None


class TestReadOnlyDetection:
    async def test_readonly_mount_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _combined(
            _df(("/", 100, 10, 10), ("/data", 100, 20, 20)),
            _df_inodes(("/", 5), ("/data", 5)),
            _mounts(("/", "rw"), ("/data", "ro")),
        ), ""))
        assert _latest_status() == "failed"
        assert _latest_metric("readonly_count") == pytest.approx(1.0)

    async def test_readonly_as_warning_when_configured(self, make_plugin, run_cycle):
        cfg = dict(BASE_CFG, readonly_is_failure=False)
        p = make_plugin(FilesystemsCollectorPlugin, cfg)
        run_cycle(p, lambda c: CmdResult(0, _combined(
            _df(("/data", 100, 20, 20)),
            _df_inodes(("/data", 5)),
            _mounts(("/data", "ro")),
        ), ""))
        assert _latest_status() == "warning"

    async def test_ignores_ro_mounts_df_does_not_report(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _combined(
            _df(("/", 100, 10, 10)),
            _df_inodes(("/", 5)),
            _mounts(("/", "rw"), ("/nix/store", "ro"),
                    ("/run/credentials/systemd-journald.service", "ro")),
        ), ""))
        assert _latest_status() == "online"
        assert _latest_metric("readonly_count") == pytest.approx(0.0)

    async def test_all_rw_stays_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _combined(
            _df(("/", 100, 10, 10)),
            _df_inodes(("/", 5)),
            _mounts(("/", "rw")),
        ), ""))
        assert _latest_status() == "online"
        assert _latest_metric("readonly_count") == pytest.approx(0.0)


class TestDegradedOutput:
    async def test_space_only_output_still_works(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _df(
            ("/", 100, 95, 95),
        ), ""))
        assert _latest_status() == "failed"
        assert _latest_metric("worst_used_pct") == pytest.approx(95.0)

    async def test_space_and_inodes_without_mounts(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _combined(
            _df(("/", 100, 10, 10)),
            _df_inodes(("/", 97)),
        ), ""))
        assert _latest_status() == "failed"
        assert _latest_metric("worst_inodes_pct") == pytest.approx(97.0)


class TestFilesystemsActions:
    async def test_on_action_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
