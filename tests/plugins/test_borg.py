import json
import re
import time
from datetime import datetime
import pytest

from vigil.plugins.borg import Borg, _frame_line
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name":       "test-borg",
    "id":         "test-borg",
    "interval":   3600,
    "max_age":    "1d",
    "repo":       "ssh://borg@host/srv/repo",
    "ssh_config": {"host": "test.host"},
}


def _latest_status(plugin_id: str):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(plugin_name: str, metric: str):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == plugin_name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S.000000")


def _list_json(epoch=None) -> str:
    archives = [] if epoch is None else [{"name": "host-2024", "start": _iso(epoch)}]
    return json.dumps({"repository": {"location": "/srv/repo"}, "archives": archives})


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Borg, BASE_CFG)


def _framed(plugin, *results):
    """The transcript the poll command's shell emits, so parse() is driven through its real input."""
    lines = []
    for index, result in enumerate(results):
        lines += [_frame_line(index, "out", result.exit_code), result.stdout,
                  _frame_line(index, "err", result.exit_code), result.stderr]
    # The shell's own exit code is the last printf's, so a borg failure is only visible inside the frames.
    return CmdResult(0, "\n".join(lines) + "\n", "")


def _collect_raw(plugin, run_cycle, result):
    """Drives a cycle with an unframed result, as a shell that died before the first borg call would give."""
    return run_cycle(plugin, lambda c: result)


def _collect(plugin, run_cycle, list_result, info_result=None):
    outputs = [list_result]
    if plugin.collect_stats:
        outputs.append(info_result if info_result is not None else CmdResult(0, "{}", ""))
    return run_cycle(plugin, lambda c: _framed(plugin, *outputs))


class TestFreshness:
    async def test_recent_archive_is_online(self, plugin, run_cycle):
        recent = int(time.time()) - 3600
        _collect(plugin, run_cycle, CmdResult(0, _list_json(recent), ""))
        assert _latest_status("test-borg") == "online"

    async def test_stale_archive_is_failed(self, plugin, run_cycle):
        stale = int(time.time()) - 3 * 24 * 3600
        _collect(plugin, run_cycle, CmdResult(0, _list_json(stale), ""))
        assert _latest_status("test-borg") == "failed"

    async def test_no_archives_is_failed(self, plugin, run_cycle):
        _collect(plugin, run_cycle, CmdResult(0, _list_json(None), ""))
        assert _latest_status("test-borg") == "failed"


def _multi_json(*epochs) -> str:
    return json.dumps({
        "repository": {
            "location": "/srv/repo",
            "last_modified": _iso(epochs[0]) if epochs else "",
        },
        "encryption": {"mode": "repokey-blake2"},
        "archives": [
            {"name": f"archive-{i}", "start": _iso(e)} for i, e in enumerate(epochs)
        ],
    })


class TestLogging:
    async def test_logs_each_archive(self, plugin, run_cycle):
        now = int(time.time())
        out = _multi_json(now - 3600, now - 90000, now - 180000)
        result = _collect(plugin, run_cycle, CmdResult(0, out, ""))
        messages = " | ".join(m for m, _ in result.logs)
        for name in ("archive-0", "archive-1", "archive-2"):
            assert name in messages

    async def test_logs_repository_metadata(self, plugin, run_cycle):
        now = int(time.time())
        result = _collect(plugin, run_cycle, CmdResult(0, _multi_json(now - 3600), ""))
        messages = " | ".join(m for m, _ in result.logs)
        assert "/srv/repo" in messages
        assert "repokey-blake2" in messages

    async def test_archives_logged_newest_first(self, plugin, run_cycle):
        now = int(time.time())
        out = _multi_json(now - 180000, now - 3600, now - 90000)
        result = _collect(plugin, run_cycle, CmdResult(0, out, ""))
        names = [m.strip().split(" ")[0] for m, _ in result.logs if m.startswith("  archive-")]
        assert names == ["archive-1", "archive-2", "archive-0"]

    async def test_logs_command_with_passphrase_redacted(self, make_plugin, run_cycle):
        p = make_plugin(Borg, {**BASE_CFG, "passphrase": "s3cret"})
        now = int(time.time())
        result = _collect(p, run_cycle, CmdResult(0, _multi_json(now - 3600), ""))
        messages = " | ".join(m for m, _ in result.logs)
        assert "borg list" in messages
        assert "BORG_PASSPHRASE=*****" in messages
        assert "s3cret" not in messages

    async def test_failure_logs_exit_code_and_hint(self, plugin, run_cycle):
        result = _collect(plugin, run_cycle, CmdResult(1, "", "sudo: borg: command not found"))
        messages = " | ".join(m for m, _ in result.logs)
        assert "exit 1" in messages
        assert "not on PATH" in messages

    async def test_publickey_failure_hints_at_ssh_key(self, plugin, run_cycle):
        result = _collect(plugin, run_cycle, CmdResult(
            2, "", "Remote: borg@heimdall.technet: Permission denied (publickey)."))
        messages = " | ".join(m for m, _ in result.logs)
        assert "ssh_key" in messages
        assert "require_sudo" not in messages

    async def test_permission_denied_hint(self, plugin, run_cycle):
        result = _collect(plugin, run_cycle, CmdResult(2, "", "Permission denied: '/srv/repo/config'"))
        assert any("require_sudo" in m for m, _ in result.logs)

    async def test_unparseable_output_logs_raw_snippet(self, plugin, run_cycle):
        result = _collect(plugin, run_cycle, CmdResult(0, "Warning: something odd", ""))
        assert any("Warning: something odd" in m for m, _ in result.logs)


class TestFailures:
    async def test_borg_error_is_failed(self, plugin, run_cycle):
        _collect(plugin, run_cycle, CmdResult(2, "", "Repository is locked"))
        assert _latest_status("test-borg") == "failed"

    async def test_unparseable_output_is_unavailable(self, plugin, run_cycle):
        _collect(plugin, run_cycle, CmdResult(0, "not json", ""))
        assert _latest_status("test-borg") == "unavailable"

    async def test_unreachable_host_is_unavailable(self, plugin, run_cycle):
        _collect(plugin, run_cycle, CmdResult(-1, "", "Agent 'h' is not connected"))
        assert _latest_status("test-borg") == "unavailable"

    async def test_missing_borg_binary_is_unavailable(self, plugin, run_cycle):
        _collect(plugin, run_cycle, CmdResult(1, "", "sudo: borg: command not found"))
        assert _latest_status("test-borg") == "unavailable"

    async def test_unreadable_repo_is_unavailable(self, plugin, run_cycle):
        _collect(plugin, run_cycle, CmdResult(2, "", "Permission denied: '/srv/repo/config'"))
        assert _latest_status("test-borg") == "unavailable"

    async def test_a_lock_timeout_is_unavailable(self, plugin, run_cycle):
        # Nothing was read, so nothing is known to be wrong with the backups themselves
        _collect(plugin, run_cycle, CmdResult(
            2, "", "Failed to create/acquire the lock /srv/repo/lock.exclusive (timeout)."))
        assert _latest_status("test-borg") == "unavailable"

    async def test_a_cache_lock_timeout_is_unavailable(self, plugin, run_cycle):
        _collect(plugin, run_cycle, CmdResult(
            2, "", "Failed to create/acquire the lock "
                   "/var/cache/vigil-borg/.cache/borg/abc/lock.exclusive (timeout)."))
        assert _latest_status("test-borg") == "unavailable"

    async def test_missing_repo_config_is_failed(self, make_plugin, run_cycle):
        cfg = {k: v for k, v in BASE_CFG.items() if k != "repo"}
        p = make_plugin(Borg, cfg)
        assert p.commands() == []
        run_cycle(p)
        assert _latest_status("test-borg") == "failed"


class TestMetrics:
    async def test_last_backup_epoch_recorded(self, plugin, run_cycle):
        epoch = int(time.time()) - 500
        _collect(plugin, run_cycle, CmdResult(0, _list_json(epoch), ""))
        assert abs(_latest_metric("test-borg", "last_backup_epoch") - epoch) <= 1

    async def test_archive_count_recorded(self, plugin, run_cycle):
        _collect(plugin, run_cycle, CmdResult(0, _list_json(int(time.time())), ""))
        assert _latest_metric("test-borg", "archive_count") == pytest.approx(1.0)


class TestCommand:
    def test_default_max_age_is_one_day(self, make_plugin):
        cfg = {k: v for k, v in BASE_CFG.items() if k != "max_age"}
        p = make_plugin(Borg, cfg)
        assert p.max_age == 86400

    def test_command_queries_newest_archive_as_json(self, make_plugin):
        p = make_plugin(Borg, BASE_CFG)
        cmd = p._list_command()
        assert "borg list" in cmd
        assert "--last 10" in cmd
        assert "--json" in cmd
        assert "ssh://borg@host/srv/repo" in cmd

    def test_command_bypasses_lock(self, make_plugin):
        p = make_plugin(Borg, BASE_CFG)
        assert "--bypass-lock" in p._list_command()

    def test_command_sets_writable_borg_base_dir(self, make_plugin):
        p = make_plugin(Borg, BASE_CFG)
        assert 'BORG_BASE_DIR="$__vigil_poll_base"' in p._list_command()
        assert "__vigil_poll_base=$(mktemp -d)" in p._poll_command()

    def test_passphrase_passed_as_env_not_argv(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "passphrase": "s3cret"})
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=" in cmd
        assert cmd.index("BORG_PASSPHRASE=") < cmd.index("borg list")

    def test_passphrase_command_uses_passcommand(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "passphrase_command": "cat /run/secret"})
        cmd = p._list_command()
        assert "BORG_PASSCOMMAND=" in cmd
        assert "BORG_PASSPHRASE=" not in cmd

    def test_passphrase_file_inlined_as_passphrase(self, make_plugin, tmp_path):
        pf = tmp_path / "borg.pass"
        pf.write_text("s3cret-from-file\n")
        p = make_plugin(Borg, {**BASE_CFG, "passphrase_file": str(pf)})
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=s3cret-from-file" in cmd
        assert "BORG_PASSCOMMAND=" not in cmd
        assert str(pf) not in cmd

    def test_passphrase_beats_passphrase_file(self, make_plugin, tmp_path):
        pf = tmp_path / "borg.pass"
        pf.write_text("from-file")
        p = make_plugin(Borg, {
            **BASE_CFG, "passphrase": "inline-wins", "passphrase_file": str(pf),
        })
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=inline-wins" in cmd
        assert "from-file" not in cmd

    def test_missing_passphrase_file_omits_env(self, make_plugin, tmp_path):
        p = make_plugin(Borg, {**BASE_CFG, "passphrase_file": str(tmp_path / "nope")})
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=" not in cmd
        assert "BORG_PASSCOMMAND=" not in cmd

    def test_no_passphrase_omits_env(self, make_plugin):
        p = make_plugin(Borg, BASE_CFG)
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=" not in cmd
        assert "BORG_PASSCOMMAND=" not in cmd

    def test_list_archives_configurable(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "list_archives": 3})
        assert "--last 3" in p._list_command()

    def test_list_archives_clamped_to_at_least_one(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "list_archives": 0})
        assert "--last 1" in p._list_command()

    def test_ssh_key_sets_borg_rsh(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "ssh_key": "/run/secrets/vigil_ssh_key"})
        cmd = p._list_command()
        assert "BORG_RSH=" in cmd
        assert "/run/secrets/vigil_ssh_key" in cmd
        assert "IdentitiesOnly=yes" in cmd
        assert "BatchMode=yes" in cmd

    def test_ssh_key_applies_to_backup_too(self, make_plugin):
        p = make_plugin(Borg, {
            **BASE_CFG, "source_paths": ["/home"], "ssh_key": "/run/secrets/k",
        })
        assert "BORG_RSH=" in p._backup_command()

    def test_rsh_overrides_ssh_key(self, make_plugin):
        p = make_plugin(Borg, {
            **BASE_CFG, "ssh_key": "/run/secrets/k", "rsh": "ssh -J jump.host",
        })
        cmd = p._list_command()
        assert "ssh -J jump.host" in cmd
        assert "/run/secrets/k" not in cmd

    def test_no_borg_rsh_without_key(self, make_plugin):
        assert "BORG_RSH=" not in make_plugin(Borg, BASE_CFG)._list_command()

    def test_borg_defaults_to_a_longer_timeout(self, make_plugin):
        from vigil.core.connectors.ssh_connector import COLLECT_TIMEOUT
        p = make_plugin(Borg, BASE_CFG)
        assert p.timeout == Borg.DEFAULT_TIMEOUT
        assert p.timeout > COLLECT_TIMEOUT

    def test_borg_timeout_is_overridable(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "timeout": "10m"})
        assert p.timeout == 600

    def test_no_sudo_by_default(self, make_plugin):
        assert "sudo" not in make_plugin(Borg, BASE_CFG)._list_command()

    def test_require_sudo_prefixes_command(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "require_sudo": True})
        cmd = p._list_command()
        assert cmd.index("sudo -n ") < cmd.index("borg list")

    def test_require_sudo_keeps_env_after_sudo(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "require_sudo": True, "passphrase": "s3cret"})
        cmd = p._list_command()
        assert cmd.index("sudo") < cmd.index("BORG_PASSPHRASE=") < cmd.index("borg list")

    def test_local_path_repo_supported(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "repo": "/mnt/backups/repo"})
        assert "/mnt/backups/repo" in p._list_command()

    def test_custom_borg_bin_and_lock_wait(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "borg_bin": "/opt/borg", "lock_wait": 30})
        cmd = p._list_command()
        assert "/opt/borg list" in cmd
        assert "--lock-wait 30" in cmd


class TestSequentialPoll:
    """Both poll calls read the same repo, so they share borg's local chunks
    cache. Dispatched as two commands the engine would run them concurrently
    and one would sit on the other's cache lock until lock_wait ran out, which
    on a slow repo failed the monitor while the backups themselves were fine."""

    def test_the_poll_is_a_single_command(self, plugin):
        assert len(plugin.commands()) == 1

    def test_list_runs_before_info(self, plugin):
        cmd = plugin._poll_command()
        assert cmd.index("borg list") < cmd.index("borg info")

    def test_the_calls_are_sequenced_not_backgrounded(self, plugin):
        assert " & " not in plugin._poll_command()

    def test_stats_off_polls_once(self, make_plugin):
        cmd = make_plugin(Borg, {**BASE_CFG, "collect_stats": False})._poll_command()
        assert "borg info" not in cmd

    async def test_each_call_keeps_its_own_exit_code(self, plugin, run_cycle):
        now = int(time.time())
        result = _collect(plugin, run_cycle, CmdResult(0, _multi_json(now - 60), ""),
                          CmdResult(2, "", "borg info exploded"))
        assert result.status == "online"
        assert any("borg info failed (exit 2)" in m for m, _ in result.logs)

    async def test_each_call_keeps_its_own_streams(self, plugin, run_cycle):
        # A borg warning on stderr must not reach the JSON parser on stdout
        now = int(time.time())
        result = _collect(plugin, run_cycle,
                          CmdResult(0, _multi_json(now - 60), "Remote: Replaying segments..."))
        assert result.status == "online"

    async def test_a_failure_before_the_first_frame_fails_the_listing(self, plugin, run_cycle):
        # sudo refusing, or the shell dying, leaves no transcript to unpack
        result = _collect_raw(plugin, run_cycle,
                              CmdResult(1, "", "sudo: a terminal is required"))
        assert result.status == "failed"
        assert any("NOPASSWD" in m for m, _ in result.logs)

    async def test_a_poll_cut_short_reports_the_call_that_never_ran(self, plugin, run_cycle):
        now = int(time.time())
        transcript = _framed(plugin, CmdResult(0, _multi_json(now - 60), ""))
        result = run_cycle(plugin, lambda c: transcript)
        assert result.status == "online"
        assert any("borg info failed" in m for m, _ in result.logs)


class TestLockHints:
    async def test_a_repo_lock_points_at_a_running_backup(self, plugin, run_cycle):
        result = _collect(plugin, run_cycle, CmdResult(
            2, "", "Failed to create/acquire the lock /srv/repo/lock.exclusive (timeout)."))
        messages = " | ".join(m for m, _ in result.logs)
        assert "a backup may be running" in messages
        assert "chunks cache" not in messages

    async def test_a_cache_lock_points_at_the_shared_cache_dir(self, plugin, run_cycle):
        result = _collect(plugin, run_cycle, CmdResult(
            2, "", "Failed to create/acquire the lock "
                   "/var/cache/vigil-borg/.cache/borg/abc123/lock.exclusive (timeout)."))
        messages = " | ".join(m for m, _ in result.logs)
        assert "chunks cache" in messages
        assert "cache_dir" in messages
        assert "a backup may be running" not in messages


class TestPollDeadline:
    """A sudo'd poll must carry its own deadline, because the agent cannot
    enforce one on it: the agent runs unprivileged, so killpg reaches only the
    wrapper it owns and reports success while the root borg keeps running."""

    def test_a_sudo_poll_is_bounded_by_a_root_side_timeout(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "require_sudo": True, "timeout": "30m"})
        assert 'timeout -k 5 "$(__vigil_poll_left)"' in p._list_command()
        assert 'timeout -k 5 "$(__vigil_poll_left)"' in p._info_command()

    def test_one_deadline_covers_the_whole_poll(self, make_plugin):
        # Given the full timeout each, two calls run in sequence could together outlast the monitor's interval
        cmd = make_plugin(Borg, {**BASE_CFG, "timeout": "30m"})._poll_command()
        assert cmd.count("+ 1800 ))") == 1
        assert cmd.count('timeout -k 5 "$(__vigil_poll_left)"') == 2

    def test_the_deadline_sits_after_sudo_so_root_owns_it(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "require_sudo": True, "timeout": "30m"})
        cmd = p._list_command()
        assert cmd.index("sudo -n") < cmd.index("timeout -k"), \
            "a timeout before sudo runs unprivileged and cannot kill a root child"

    def test_the_deadline_follows_the_env_assignments(self, make_plugin):
        # sudo reads leading VAR=val as assignments and the first non-assignment as the command
        p = make_plugin(Borg, {**BASE_CFG, "require_sudo": True, "timeout": "30m",
                               "passphrase_command": "cat /k"})
        cmd = p._list_command()
        assert cmd.index("BORG_PASSCOMMAND=") < cmd.index("timeout -k")

    def test_a_backup_is_not_bounded_by_the_poll_deadline(self, make_plugin):
        # Backups are launched detached precisely so they outlive the collect cycle
        p = make_plugin(Borg, {**BASE_CFG, "require_sudo": True, "timeout": "30m"})
        head = p._backup_command().split("borg")[0]
        assert "timeout" not in head


class TestActions:
    async def test_no_actions_without_source_paths(self, plugin):
        assert plugin.get_actions() == []

    async def test_backup_actions_exposed_with_source_paths(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "source_paths": ["/home"]})
        ids = {a['action_id'] for a in p.get_actions()}
        assert ids == {"run_backup", "dry_run_backup"}

    async def test_unknown_action_returns_false(self, plugin):
        assert plugin.plan_action("nonsense") is None

    async def test_backup_without_source_paths_fails(self, plugin):
        plan = plugin.plan_action("run_backup")
        assert plan.success is False
        assert any("source_paths" in m for m, _ in plan.logs)


BACKUP_CFG = {**BASE_CFG, "source_paths": ["/home", "/etc"]}


class TestBackupCommand:
    def test_includes_sources_and_repo_archive(self, make_plugin):
        cmd = make_plugin(Borg, BACKUP_CFG)._backup_command()
        assert "borg create" in cmd
        assert "/home" in cmd and "/etc" in cmd
        assert "ssh://borg@host/srv/repo::" in cmd

    def test_archive_name_uses_prefix_and_is_sortable(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "archive_prefix": "nightly"})
        name = p.default_archive_name()
        assert name.startswith("nightly-")
        assert re.match(r"nightly-\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", name)

    def test_archive_prefix_defaults_to_monitor_name(self, make_plugin):
        p = make_plugin(Borg, BACKUP_CFG)
        assert p.default_archive_name().startswith("test-borg-")

    def test_excludes_are_passed(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "exclude": ["/home/*/.cache", "*.tmp"]})
        cmd = p._backup_command()
        assert cmd.count("--exclude ") >= 2
        assert "/home/*/.cache" in cmd
        assert "*.tmp" in cmd

    def test_exclude_accepts_bare_string(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "exclude": "/tmp"})
        assert p.exclude == ["/tmp"]
        assert "/tmp" in p._backup_command()

    def test_source_paths_accepts_bare_string(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "source_paths": "/home"})
        assert p.source_paths == ["/home"]

    def test_exclude_from_file_passed(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "exclude_from": "/etc/borg.excludes"})
        assert "--exclude-from /etc/borg.excludes" in p._backup_command()

    def test_exclude_if_present_markers(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "exclude_if_present": [".nobackup"]})
        assert "--exclude-if-present .nobackup" in p._backup_command()

    def test_one_file_system_on_by_default(self, make_plugin):
        assert "--one-file-system" in make_plugin(Borg, BACKUP_CFG)._backup_command()

    def test_one_file_system_can_be_disabled(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "one_file_system": False})
        assert "--one-file-system" not in p._backup_command()

    def test_exclude_caches_on_by_default(self, make_plugin):
        assert "--exclude-caches" in make_plugin(Borg, BACKUP_CFG)._backup_command()

    def test_compression_configurable(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "compression": "zstd,10"})
        assert "--compression zstd,10" in p._backup_command()

    def test_default_compression_is_zstd(self, make_plugin):
        assert "--compression zstd" in make_plugin(Borg, BACKUP_CFG)._backup_command()

    def test_backup_uses_persistent_cache_dir(self, make_plugin):
        cmd = make_plugin(Borg, BACKUP_CFG)._backup_command()
        assert "BORG_BASE_DIR=/var/cache/vigil-borg" in cmd
        assert "mktemp" not in cmd

    def test_cache_dir_configurable(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "cache_dir": "/srv/borgcache"})
        assert "BORG_BASE_DIR=/srv/borgcache" in p._backup_command()

    def test_poll_still_uses_throwaway_base_dir(self, make_plugin):
        assert "$(mktemp -d)" in make_plugin(Borg, BACKUP_CFG)._poll_command()

    def test_a_configured_cache_dir_still_gets_a_scratch_dir(self, make_plugin):
        # The scratch dir also holds each call's captured output, so it is needed either way
        cmd = make_plugin(Borg, {**BASE_CFG, "cache_dir": "/srv/borgcache"})._poll_command()
        assert "$(mktemp -d)" in cmd
        assert "BORG_BASE_DIR=/srv/borgcache" in cmd
        assert 'BORG_BASE_DIR="$__vigil_poll_base"' not in cmd

    def test_configured_cache_dir_applies_to_polls(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "cache_dir": "/srv/borgcache"})
        for cmd in (p._list_command(), p._info_command()):
            assert "BORG_BASE_DIR=/srv/borgcache" in cmd
            assert "mktemp" not in cmd
            assert "rm -rf" not in cmd

    def test_poll_removes_throwaway_base_dir(self, make_plugin):
        cmd = make_plugin(Borg, BACKUP_CFG)._poll_command()
        assert "trap 'rm -rf" in cmd
        assert cmd.index("mktemp -d") < cmd.index("BORG_BASE_DIR=")

    def test_backup_does_not_remove_persistent_cache_dir(self, make_plugin):
        assert "rm -rf" not in make_plugin(Borg, BACKUP_CFG)._backup_command()

    def test_backup_uses_long_lock_wait(self, make_plugin):
        assert "--lock-wait 600" in make_plugin(Borg, BACKUP_CFG)._backup_command()

    def test_backup_lock_wait_configurable(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "backup_lock_wait": 30})
        assert "--lock-wait 30" in p._backup_command()

    def test_backup_emits_structured_progress(self, make_plugin):
        cmd = make_plugin(Borg, BACKUP_CFG)._backup_command()
        assert "--log-json" in cmd
        assert "--progress" in cmd

    def test_dry_run_omits_stats_and_adds_flag(self, make_plugin):
        cmd = make_plugin(Borg, BACKUP_CFG)._backup_command(dry_run=True)
        assert "--dry-run" in cmd
        assert "--stats" not in cmd

    def test_real_backup_includes_stats(self, make_plugin):
        cmd = make_plugin(Borg, BACKUP_CFG)._backup_command(dry_run=False)
        assert "--stats" in cmd
        assert "--dry-run" not in cmd

    def test_passphrase_inlined_for_backup(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "passphrase": "s3cret"})
        assert "BORG_PASSPHRASE=s3cret" in p._backup_command()

    def test_backup_honours_require_sudo(self, make_plugin):
        p = make_plugin(Borg, {**BACKUP_CFG, "require_sudo": True})
        assert p._backup_command().startswith("sudo -n ")


def _poll(size, exit_code, alive, out):
    """Build the raw stdout one poll_command produces on the target."""
    return CmdResult(0, (
        f"===VIGIL_SIZE===\n{size}\n"
        f"===VIGIL_EXIT===\n{exit_code if exit_code is not None else ''}\n"
        f"===VIGIL_ALIVE===\n{1 if alive else 0}\n"
        f"===VIGIL_OUT===\n{out}"
    ), "")


@pytest.fixture
def backup_plugin(make_plugin):
    return make_plugin(Borg, BACKUP_CFG)


async def _launch(plugin, action_id="run_backup", pid=4242):
    """Drive plan_action -> ActionPlan (launch) -> interpret_action, mirroring
    VigilEngine.dispatch_action for a launched detached job. Fakes the launch
    command's stdout as the remote pid."""
    plan = plugin.plan_action(action_id)
    if plan is None:
        return None
    if hasattr(plan, "success"):        # CollectResult (refused) — no launch
        plugin.storage.apply(plan)
        return plan
    launch_result = CmdResult(0, f"{pid}\n", "")
    outcome = plugin.interpret_action(action_id, launch_result)
    plugin.storage.apply(outcome)
    return outcome


def _poll_once(plugin, poll_result):
    """Drive one monitor cycle while a job runs: commands() emits the poll
    command, parse() advances the job from the poll's (faked) output."""
    cmds = plugin.commands()
    assert len(cmds) == 1                # a running job polls with one command
    result = plugin.parse([poll_result])
    plugin.storage.apply(result)
    return result


class TestBackupLaunch:
    async def test_launch_records_running_job(self, backup_plugin, db_manager):
        outcome = await _launch(backup_plugin, pid=1234)
        assert outcome.success is True
        job = backup_plugin.jobs.running()
        assert job is not None
        assert job['kind'] == 'backup'
        assert job['pid'] == 1234
        assert job['state'] == 'running'

    async def test_dry_run_launch_records_its_kind(self, backup_plugin):
        await _launch(backup_plugin, "dry_run_backup")
        assert backup_plugin.jobs.running()['kind'] == 'dry-run'

    async def test_launch_failure_records_no_job(self, backup_plugin):
        plan = backup_plugin.plan_action("run_backup")
        outcome = backup_plugin.interpret_action("run_backup", CmdResult(1, "", "boom"))
        backup_plugin.storage.apply(outcome)
        assert outcome.success is False
        assert backup_plugin.jobs.running() is None

    async def test_launched_command_is_redacted(self, backup_plugin, db_manager):
        backup_plugin.passphrase = "s3cret"
        await _launch(backup_plugin)
        stored = backup_plugin.jobs.running()['command']
        assert "s3cret" not in stored
        assert "BORG_PASSPHRASE=*****" in stored

    async def test_second_backup_refused_while_running(self, backup_plugin):
        await _launch(backup_plugin)
        refused = backup_plugin.plan_action("run_backup")
        assert hasattr(refused, "success")           # a CollectResult, not a launch
        assert any("already running" in m for m, _ in refused.logs)


class TestBackupPolling:
    async def test_poll_while_running_keeps_job_running(self, backup_plugin, db_manager):
        await _launch(backup_plugin)
        _poll_once(backup_plugin, _poll(10, None, True, "working\n"))
        assert backup_plugin.jobs.running() is not None

    async def test_unanswered_poll_leaves_the_job_running(self, backup_plugin, db_manager):
        await _launch(backup_plugin)
        result = _poll_once(backup_plugin, CmdResult(-1, "", "Agent 'x' is not connected"))
        assert backup_plugin.jobs.running() is not None
        assert any("Could not poll" in msg for msg, _ in result.logs)

    async def test_job_started_mid_cycle_does_not_swallow_the_listing(self, backup_plugin, db_manager):
        cmds = backup_plugin.commands()                    # asked for a repo listing: no job yet
        assert len(cmds) >= 1
        await _launch(backup_plugin)                       # the button lands before parse
        result = backup_plugin.parse([CmdResult(1, "", "repo busy")] * len(cmds))
        backup_plugin.storage.apply(result)
        assert backup_plugin.jobs.running() is not None   # the job was not judged by listing output

    async def test_poll_completion_marks_succeeded(self, backup_plugin, db_manager):
        await _launch(backup_plugin)
        _poll_once(backup_plugin, _poll(20, 0, False, "done\n"))
        assert backup_plugin.jobs.running() is None
        assert backup_plugin.jobs.recent()[0]['state'] == 'succeeded'

    async def test_borg_warning_exit_is_still_success(self, backup_plugin):
        await _launch(backup_plugin)
        _poll_once(backup_plugin, _poll(5, 1, False, ""))
        assert backup_plugin.jobs.recent()[0]['state'] == 'succeeded'

    async def test_borg_error_exit_is_failure(self, backup_plugin):
        await _launch(backup_plugin)
        _poll_once(backup_plugin, _poll(5, 2, False, ""))
        job = backup_plugin.jobs.recent()[0]
        assert job['state'] == 'failed'
        assert job['exit_code'] == 2

    async def test_dead_process_without_exit_file_fails(self, backup_plugin):
        await _launch(backup_plugin)
        _poll_once(backup_plugin, _poll(5, None, False, "partial"))
        assert backup_plugin.jobs.recent()[0]['state'] == 'failed'

    async def test_progress_parsed_from_log_json_on_poll(self, backup_plugin, db_manager):
        await _launch(backup_plugin)
        progress = json.dumps({
            "type": "archive_progress", "path": "/home/user/file.txt",
            "original_size": 1048576, "deduplicated_size": 524288, "nfiles": 42,
        })
        _poll_once(backup_plugin, _poll(len(progress) + 1, None, True, progress + "\n"))
        job = backup_plugin.jobs.running()
        assert "42 files" in job['progress']
        assert "/home/user/file.txt" in job['progress']

    async def test_zero_counter_progress_is_ignored(self, backup_plugin):
        await _launch(backup_plugin)
        real = json.dumps({
            "original_size": 1048576, "deduplicated_size": 524288, "nfiles": 42,
            "path": "src/big.bin", "type": "archive_progress", "finished": False,
        })
        opening = json.dumps({
            "original_size": 0, "deduplicated_size": 0, "nfiles": 0,
            "path": "src", "type": "archive_progress", "finished": False,
        })
        _poll_once(backup_plugin, _poll(len(real) + 1, None, True, real + "\n"))
        assert "42 files" in backup_plugin.jobs.running()['progress']
        # a subsequent zero-counter line must not clobber the real summary
        off = backup_plugin.jobs.running()['output_seq']
        _poll_once(backup_plugin, _poll(off + len(opening) + 1, None, True, opening + "\n"))
        assert "42 files" in backup_plugin.jobs.running()['progress']

    async def test_output_lines_are_stored(self, backup_plugin, db_manager):
        await _launch(backup_plugin)
        _poll_once(backup_plugin, _poll(12, 0, False, "line1\nline2\n"))
        job_id = backup_plugin.jobs.recent()[0]['id']
        assert [o['message'] for o in db_manager.job_output(job_id)] == ["line1", "line2"]

    async def test_partial_line_completed_across_polls(self, backup_plugin, db_manager):
        await _launch(backup_plugin)
        # first poll delivers a line with no trailing newline yet
        _poll_once(backup_plugin, _poll(4, None, True, "par"))
        job_id = backup_plugin.jobs.running()['id']
        assert db_manager.job_output(job_id) == []       # nothing consumed yet
        # next poll re-reads from offset 0 and completes the line
        _poll_once(backup_plugin, _poll(8, 0, False, "partial\n"))
        assert [o['message'] for o in db_manager.job_output(job_id)] == ["partial"]


def _info_json(total=1000, csize=500, unique=250) -> str:
    return json.dumps({
        "cache": {"stats": {
            "total_size": total,
            "total_csize": csize,
            "unique_csize": unique,
            "total_chunks": 100,
            "total_unique_chunks": 50,
        }},
        "repository": {"location": "/srv/repo"},
    })


class TestRepoStats:
    async def test_info_command_requests_json(self, make_plugin):
        cmd = make_plugin(Borg, BASE_CFG)._info_command()
        assert "borg info" in cmd
        assert "--json" in cmd

    async def test_stats_recorded_as_metrics(self, plugin, run_cycle):
        now = int(time.time())
        _collect(plugin, run_cycle, CmdResult(0, _list_json(now - 60), ""), CmdResult(0, _info_json(), ""))

        assert _latest_metric("test-borg", "original_size") == pytest.approx(1000)
        assert _latest_metric("test-borg", "deduplicated_size") == pytest.approx(250)

    async def test_dedup_ratio_is_derived(self, plugin, run_cycle):
        now = int(time.time())
        _collect(plugin, run_cycle, CmdResult(0, _list_json(now - 60), ""),
                CmdResult(0, _info_json(total=1000, unique=250), ""))
        assert _latest_metric("test-borg", "dedup_ratio") == pytest.approx(4.0)

    async def test_stats_disabled_skips_info_call(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "collect_stats": False})
        assert len(p.commands()) == 1

    async def test_info_failure_does_not_fail_the_monitor(self, plugin, run_cycle):
        now = int(time.time())
        _collect(plugin, run_cycle, CmdResult(0, _list_json(now - 60), ""), CmdResult(2, "", "info exploded"))
        assert _latest_status("test-borg") == "online"

    async def test_unparseable_info_is_tolerated(self, plugin, run_cycle):
        now = int(time.time())
        _collect(plugin, run_cycle, CmdResult(0, _list_json(now - 60), ""), CmdResult(0, "not json", ""))
        assert _latest_status("test-borg") == "online"

    async def test_empty_repo_skips_the_stats_call(self, plugin, run_cycle):
        _collect(plugin, run_cycle, CmdResult(0, _list_json(None), ""))
        assert _latest_status("test-borg") == "failed"

    async def test_missing_cache_stats_yields_no_metrics(self, plugin, run_cycle):
        now = int(time.time())
        _collect(plugin, run_cycle, CmdResult(0, _list_json(now - 60), ""),
                CmdResult(0, json.dumps({"repository": {}}), ""))
        assert _latest_metric("test-borg", "original_size") is None


class TestEventLogging:
    async def test_messages_land_where_the_ui_reads_them(self, plugin, run_cycle, db_manager):
        from vigil.core.database.database import Event, LogLine
        _collect(plugin, run_cycle, CmdResult(0, _list_json(int(time.time()) - 60), ""))
        db_manager.flush()

        with db.connection_context():
            events = Event.select().where(Event.message.startswith("[test-borg] ")).count()
            loglines = LogLine.select().where(LogLine.plugin_id == "test-borg").count()

        assert events > 0, "plugin wrote no events for the panel to show"
        assert loglines == 0, "plugin does not collect target logs; panel must read Events"


class TestArchiveCache:
    async def test_archives_cached_for_ui(self, make_plugin, run_cycle):
        p = make_plugin(Borg, {**BASE_CFG, "collect_stats": False})
        now = int(time.time())
        run_cycle(p, lambda c: CmdResult(0, _multi_json(now - 3600, now - 7200), ""))

        archives, info = p.cached_archives()
        assert [a['name'] for a in archives] == ["archive-0", "archive-1"]
        assert info['location'] == "/srv/repo"

    async def test_cached_archives_empty_before_first_poll(self, plugin):
        assert plugin.cached_archives() == ([], {})

    async def test_archive_sizes_merged_from_info(self, plugin, run_cycle):
        now = int(time.time())
        info = json.dumps({
            "cache": {"stats": {"total_size": 1000, "total_csize": 500,
                                "unique_csize": 250}},
            "archives": [
                {"name": "archive-0", "stats": {
                    "original_size": 400000, "compressed_size": 300000,
                    "deduplicated_size": 1024, "nfiles": 42}},
            ],
        })
        _collect(plugin, run_cycle, CmdResult(0, _multi_json(now - 3600), ""), CmdResult(0, info, ""))

        archives, _ = plugin.cached_archives()
        first = next(a for a in archives if a["name"] == "archive-0")
        assert first["original"] == pytest.approx(400000)
        assert first["deduplicated"] == pytest.approx(1024)
        assert first["nfiles"] == pytest.approx(42)

    async def test_info_command_requests_per_archive_stats(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "list_archives": 5})
        assert "--last 5" in p._info_command()

    async def test_archives_without_stats_keep_names(self, plugin, run_cycle):
        now = int(time.time())
        info = json.dumps({"cache": {"stats": {"total_size": 10,
                                               "unique_csize": 5}}})
        _collect(plugin, run_cycle, CmdResult(0, _multi_json(now - 3600), ""), CmdResult(0, info, ""))

        archives, _ = plugin.cached_archives()
        assert [a["name"] for a in archives] == ["archive-0"]
        assert "original" not in archives[0]


CANARY_CFG = {**BASE_CFG, "canary_path": "/Storage/System/.vigil-canary", "collect_stats": False}


def _cycle_with(plugin, run_cycle, **extra):
    """Drives one poll, answering each planned call from `extra` by name and the listing with a fresh archive."""
    listing = CmdResult(0, _list_json(int(time.time()) - 60), "")
    answers = {'list': listing, 'info': CmdResult(0, "{}", ""), **extra}
    return run_cycle(plugin, lambda c: _framed(plugin, *[answers[n] for n in plugin._poll_calls]))


class TestRestoreCanary:
    async def test_no_canary_before_an_archive_is_known(self, make_plugin):
        p = make_plugin(Borg, CANARY_CFG)
        p.commands()
        assert p._poll_calls == ['list']

    async def test_canary_runs_once_the_newest_archive_is_known(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CANARY_CFG)
        _cycle_with(p, run_cycle)
        cmd = p.commands()[0].text
        assert p._poll_calls == ['list', 'canary', 'canary_live']
        assert "extract --stdout" in cmd
        assert "host-2024 Storage/System/.vigil-canary" in cmd.replace("'", "")

    async def test_matching_canary_passes(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CANARY_CFG)
        _cycle_with(p, run_cycle)
        p.commands()
        _cycle_with(p, run_cycle, canary=CmdResult(0, "token", ""), canary_live=CmdResult(0, "token\n", ""))
        assert _latest_metric("test-borg", "canary_ok") == 1.0
        assert _latest_status("test-borg") == "online"

    async def test_differing_canary_fails_the_monitor(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CANARY_CFG)
        _cycle_with(p, run_cycle)
        p.commands()
        _cycle_with(p, run_cycle, canary=CmdResult(0, "old", ""), canary_live=CmdResult(0, "new", ""))
        assert _latest_metric("test-borg", "canary_ok") == 0.0
        assert _latest_status("test-borg") == "failed"

    async def test_a_failed_canary_keeps_the_monitor_failed_until_it_passes(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CANARY_CFG)
        _cycle_with(p, run_cycle)
        p.commands()
        _cycle_with(p, run_cycle, canary=CmdResult(1, "", "Include pattern 'x' never matched."),
                    canary_live=CmdResult(0, "t", ""))
        p.commands()
        assert 'canary' not in p._poll_calls
        _cycle_with(p, run_cycle)
        assert _latest_status("test-borg") == "failed"

    async def test_a_locked_repo_reaches_no_verdict(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CANARY_CFG)
        _cycle_with(p, run_cycle)
        p.commands()
        _cycle_with(p, run_cycle, canary=CmdResult(2, "", "Failed to create/acquire the lock /r/lock (timeout)."),
                    canary_live=CmdResult(0, "t", ""))
        assert _latest_metric("test-borg", "canary_ok") is None
        assert _latest_status("test-borg") == "online"

    async def test_verify_restore_action_records_the_verdict(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CANARY_CFG)
        _cycle_with(p, run_cycle)
        plan = p.plan_action("verify_restore")
        assert "extract --stdout" in plan.command
        outcome = p.interpret_action("verify_restore", _framed(p, CmdResult(0, "t", ""), CmdResult(0, "t", "")))
        p.storage.apply(outcome)
        assert outcome.success is True
        assert outcome.status is None
        assert _latest_metric("test-borg", "canary_ok") == 1.0


CHECK_CFG = {**BASE_CFG, "repo": "/srv/local", "check_units": ["borgmatic-check.service"], "collect_stats": False}


def _checks_out(ok=None, end=None, start=None, errs=()):
    def line(epoch, text):
        return f"{epoch}.000000 host systemd[1]: {text}" if epoch else ""
    lines = ["unit=borgmatic-check.service",
             "ok=" + line(ok, "Finished Checks."),
             "end=" + (line(end, "borgmatic-check.service: Failed with result 'exit-code'.") if end else line(ok, "Finished Checks.")),
             "start=" + line(start, "Starting Checks...")]
    lines += [f"err={epoch}.0 host borgmatic[9]: Command 'borg check --info {repo}' returned non-zero exit status 1." for epoch, repo in errs]
    return CmdResult(0, "\n".join(lines), "")


class TestCheckFreshness:
    async def test_recent_success_passes(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CHECK_CFG)
        p.commands()
        _cycle_with(p, run_cycle, checks=_checks_out(ok=int(time.time()) - 3600))
        assert _latest_metric("test-borg", "checks_ok") == 1.0
        assert _latest_status("test-borg") == "online"

    async def test_stale_success_fails(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CHECK_CFG)
        p.commands()
        _cycle_with(p, run_cycle, checks=_checks_out(ok=int(time.time()) - 10 * 86400))
        assert _latest_status("test-borg") == "failed"

    async def test_no_record_fails(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CHECK_CFG)
        p.commands()
        _cycle_with(p, run_cycle, checks=_checks_out())
        assert _latest_status("test-borg") == "failed"

    async def test_a_failure_naming_this_repo_fails(self, make_plugin, run_cycle):
        now = int(time.time())
        p = make_plugin(Borg, CHECK_CFG)
        p.commands()
        _cycle_with(p, run_cycle, checks=_checks_out(ok=now - 86400, end=now - 60, start=now - 600,
                                                     errs=[(now - 70, "/srv/local")]))
        assert _latest_status("test-borg") == "failed"

    async def test_a_failure_naming_only_another_repo_counts_as_a_pass(self, make_plugin, run_cycle):
        now = int(time.time())
        p = make_plugin(Borg, CHECK_CFG)
        p.commands()
        _cycle_with(p, run_cycle, checks=_checks_out(end=now - 60, start=now - 600,
                                                     errs=[(now - 70, "ssh://borg@far/srv/local")]))
        assert _latest_metric("test-borg", "checks_ok") == 1.0
        assert _latest_status("test-borg") == "online"

    async def test_a_rotated_journal_keeps_the_last_success(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CHECK_CFG)
        p.commands()
        _cycle_with(p, run_cycle, checks=_checks_out(ok=int(time.time()) - 3600))
        p.commands()
        _cycle_with(p, run_cycle, checks=_checks_out())
        assert _latest_status("test-borg") == "online"

    async def test_the_probe_reads_only_systemds_own_lines(self, make_plugin):
        p = make_plugin(Borg, CHECK_CFG)
        p.commands()
        assert "_PID=1" in p._poll_command()


PRUNE_OUTPUT = """Keeping archive (rule: daily #1):            backup-2026-09-23T00:00:00           Tue, 2026-09-22 19:00:00 [d0e5]
Would prune:                                 backup-2026-09-22T00:00:00           Mon, 2026-09-21 19:00:00 [43a8]
Keeping archive (rule: weekly #1):           backup-2026-09-21T00:00:00           Sun, 2026-09-20 19:00:00 [c598]"""


class TestPrunePreview:
    async def test_refused_without_retention(self, plugin):
        outcome = plugin.plan_action("prune_preview")
        assert outcome.success is False and outcome.status is None

    async def test_is_a_dry_run_with_the_configured_policy(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "keep_daily": 7, "keep_within": "6H", "archive_prefix": "odin"})
        cmd = p.plan_action("prune_preview").command
        assert "prune --dry-run --list" in cmd
        assert "--keep-daily 7" in cmd and "--keep-within 6H" in cmd
        assert "--glob-archives 'odin-*'" in cmd

    async def test_lists_what_would_go(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "keep_daily": 1})
        outcome = p.interpret_action("prune_preview", _framed(p, CmdResult(0, "", PRUNE_OUTPUT)))
        content = outcome.metadata['content']
        assert "Would prune 1 of 3" in content
        assert "backup-2026-09-22T00:00:00" in content.split("Keeping")[0]

    async def test_failure_leaves_the_status_alone(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "keep_daily": 1})
        outcome = p.interpret_action("prune_preview", _framed(p, CmdResult(2, "", "Failed to create/acquire the lock")))
        assert outcome.success is False and outcome.status is None
        assert "locked" in outcome.metadata['content']


LISTING = "\n".join([
    "d\t0\t2026-09-20T10:00:00.000000\tStorage/System/etc",
    "-\t2048\t2026-09-20T10:01:00.000000\tStorage/System/notes.txt",
    "d\t\t\tStorage/System/var",
])


def _run_filter(member: str, lines: str, tmp_path) -> str:
    """Runs the children filter the browse command uses over a borg listing, in a real shell."""
    import subprocess
    from vigil.plugins.borg.parsing import _children_filter
    listing = tmp_path / "listing"
    listing.write_text(lines + "\n")
    return subprocess.run(["sh", "-c", f"{_children_filter(member)} {listing}"],
                          capture_output=True, text=True, check=True).stdout


class TestBrowse:
    async def test_lists_the_path_through_the_children_filter(self, plugin):
        cmd = plugin.plan_action("browse_archive", archive="a1", path="/Storage/System/").command
        assert "::a1 Storage/System" in cmd.replace("'", "")
        assert "awk -v p=Storage/System/" in cmd

    async def test_top_level_lists_the_whole_archive(self, plugin):
        cmd = plugin.plan_action("browse_archive", archive="a1", path="").command
        assert "::a1 --format" not in cmd and "awk -v p=''" in cmd

    async def test_filter_keeps_direct_children_only(self, tmp_path):
        out = _run_filter("Storage", "\n".join([
            "d\t0\t2026-09-20T10:00:00\tStorage",
            "-\t5\t2026-09-20T10:00:00\tStorage/a.txt",
            "-\t5\t2026-09-20T10:00:00\tStorage/deep/b.txt",
        ]), tmp_path)
        assert out.splitlines() == ["-\t5\t2026-09-20T10:00:00\tStorage/a.txt", "d\t\t\tStorage/deep"]

    async def test_filter_makes_up_parents_borg_never_stored(self, tmp_path):
        out = _run_filter("", "\n".join([
            "-\t5\t2026-09-20T10:00:00\tStorage/System/a",
            "-\t5\t2026-09-20T10:00:00\tStorage/System/b",
            "-\t5\t2026-09-20T10:00:00\thome/x",
        ]), tmp_path)
        assert out.splitlines() == ["d\t\t\tStorage", "d\t\t\thome"]

    async def test_filter_keeps_a_path_with_a_tab(self, tmp_path):
        out = _run_filter("", "-\t5\t2026-09-20T10:00:00\ta\tb", tmp_path)
        assert out.splitlines() == ["-\t5\t2026-09-20T10:00:00\ta\tb"]

    async def test_listing_is_parsed_and_cached(self, plugin):
        outcome = plugin.interpret_action("browse_archive", _framed(plugin, CmdResult(0, LISTING, "")),
                                          archive="a1", path="Storage/System")
        assert outcome.success is True
        entries = plugin.listing("a1", "/Storage/System/")
        assert [e['name'] for e in entries] == ["etc", "var", "notes.txt"]
        assert entries[2]['size'] == 2048 and entries[2]['mtime'] == "2026-09-20 10:01"

    async def test_a_cached_folder_is_served_without_borg(self, plugin):
        plugin.interpret_action("browse_archive", _framed(plugin, CmdResult(0, LISTING, "")),
                                archive="a1", path="Storage/System")
        outcome = plugin.plan_action("browse_archive", archive="a1", path="Storage/System")
        assert outcome.success is True and "3 entries" in outcome.metadata['content']

    async def test_a_real_item_beats_a_made_up_folder(self):
        from vigil.plugins.borg.parsing import _parse_listing
        entries = _parse_listing("d\t\t\tx/etc\nd\t0\t2026-09-20T10:00:00\tx/etc")
        assert len(entries) == 1 and entries[0]['mtime'] == "2026-09-20 10:00"

    async def test_output_is_capped(self, make_plugin):
        p = make_plugin(Borg, {**BASE_CFG, "browse_limit": 2})
        assert "head -n 3" in p.plan_action("browse_archive", archive="a1", path="").command
        lines = "\n".join(f"-\t1\t\t{n}" for n in "abc")
        outcome = p.interpret_action("browse_archive", _framed(p, CmdResult(0, lines, "")), archive="a1", path="")
        assert "first 2 entries" in outcome.metadata['content']
        assert "\nc " not in outcome.metadata['content']

    async def test_climbing_paths_are_refused(self, plugin):
        assert plugin.plan_action("browse_archive", archive="a1", path="x/../../etc").success is False

    async def test_a_failed_listing_is_not_cached(self, plugin):
        plugin.interpret_action("browse_archive", _framed(plugin, CmdResult(2, "", "boom")), archive="a1", path="")
        assert plugin.listing("a1", "") is None


RESTORE_CFG = {**BASE_CFG, "restore_dir": "/var/tmp/r", "require_sudo": True}


class TestRestore:
    async def test_extracts_into_a_new_folder_under_restore_dir(self, make_plugin):
        p = make_plugin(Borg, RESTORE_CFG)
        plan = p.plan_action("restore_archive", archive="a1", path="/Storage/System/x")
        assert "sudo -n mkdir -p /var/tmp/r/a1-" in plan.command
        assert "extract" in plan.command and "::a1 Storage/System/x" in plan.command.replace("'", "")

    async def test_launch_records_a_restore_job(self, make_plugin):
        p = make_plugin(Borg, RESTORE_CFG)
        p.plan_action("restore_archive", archive="a1", path="etc")
        outcome = p.interpret_action("restore_archive", CmdResult(0, "77\n", ""))
        p.storage.apply(outcome)
        assert p.jobs.running()['kind'] == 'restore'
        assert "/var/tmp/r/a1-" in outcome.metadata['content']

    async def test_whole_archive_restores_are_refused(self, plugin):
        assert plugin.plan_action("restore_archive", archive="a1", path="/").success is False

    async def test_refused_while_a_job_runs(self, make_plugin):
        p = make_plugin(Borg, RESTORE_CFG)
        p.plan_action("restore_archive", archive="a1", path="etc")
        p.storage.apply(p.interpret_action("restore_archive", CmdResult(0, "77\n", "")))
        assert p.plan_action("restore_archive", archive="a1", path="etc").success is False

    async def test_restores_several_ticked_paths_at_once(self, make_plugin):
        p = make_plugin(Borg, RESTORE_CFG)
        plan = p.plan_action("restore_archive", archive="a1", paths=["/etc/x", "home/y"])
        assert "::a1 etc/x home/y" in plan.command.replace("'", "")

    async def test_one_climbing_path_refuses_the_lot(self, make_plugin):
        p = make_plugin(Borg, RESTORE_CFG)
        assert p.plan_action("restore_archive", archive="a1", paths=["etc", "../x"]).success is False

    async def test_extract_progress_becomes_the_summary(self):
        line = json.dumps({"type": "progress_percent", "message": " 50.0% Extracting: etc/x", "finished": False})
        assert Borg._progress_from_lines([line]) == "50.0% Extracting: etc/x"


class TestBackupSummary:
    async def test_reports_freshness_and_extras(self, make_plugin, run_cycle):
        p = make_plugin(Borg, CANARY_CFG)
        _cycle_with(p, run_cycle)
        summary = p.backup_summary()
        assert summary['fresh'] is True
        assert summary['restore_check'] is None
        assert summary['repo_checks'] is None


def _listed(plugin, run_cycle, *names):
    """Runs a poll that lists these archives, newest first."""
    now = int(time.time())
    archives = [{"name": n, "start": _iso(now - 3600 * (i + 1))} for i, n in enumerate(names)]
    _collect(plugin, run_cycle, CmdResult(0, json.dumps({"archives": archives}), ""))


DELETE_CFG = {**BASE_CFG, "allow_delete": True, "keep_daily": 7}


class TestMaintenance:
    @pytest.mark.parametrize("action_id, kind, args", [
        ("check_repo", "check", "check --progress"),
        ("compact_repo", "compact", "compact --progress"),
        ("prune_repo", "prune", "prune --list --stats --glob-archives"),
    ])
    async def test_runs_as_a_detached_job(self, make_plugin, action_id, kind, args):
        p = make_plugin(Borg, DELETE_CFG)
        plan = p.plan_action(action_id)
        assert args in plan.command and "--log-json" in plan.command
        p.storage.apply(p.interpret_action(action_id, CmdResult(0, "91\n", "")))
        assert p.jobs.running()['kind'] == kind

    async def test_prune_applies_the_retention_policy(self, make_plugin):
        cmd = make_plugin(Borg, DELETE_CFG).plan_action("prune_repo").command
        assert "--keep-daily 7" in cmd and "--dry-run" not in cmd

    async def test_verify_data_is_opt_in(self, make_plugin):
        assert "--verify-data" not in make_plugin(Borg, BASE_CFG).plan_action("check_repo").command
        assert "--verify-data" in make_plugin(Borg, {**BASE_CFG, "check_verify_data": True}).plan_action("check_repo").command

    @pytest.mark.parametrize("action_id", ["prune_repo", "delete_archive", "break_lock"])
    async def test_destructive_actions_are_off_by_default(self, make_plugin, action_id):
        outcome = make_plugin(Borg, {**BASE_CFG, "keep_daily": 7}).plan_action(action_id, archive="a1")
        assert outcome.success is False and "allow_delete" in outcome.metadata['content']

    async def test_one_job_at_a_time(self, make_plugin):
        p = make_plugin(Borg, DELETE_CFG)
        p.plan_action("check_repo")
        p.storage.apply(p.interpret_action("check_repo", CmdResult(0, "91\n", "")))
        assert p.plan_action("compact_repo").success is False
        assert p.plan_action("break_lock").success is False

    async def test_break_lock_runs_directly(self, make_plugin):
        p = make_plugin(Borg, DELETE_CFG)
        assert "break-lock" in p.plan_action("break_lock").command
        assert p.interpret_action("break_lock", _framed(p, CmdResult(0, "", ""))).success is True

    async def test_check_progress_messages_become_the_summary(self):
        line = json.dumps({"type": "progress_message", "message": "Checking segments 12.0%", "finished": False})
        assert Borg._progress_from_lines([line]) == "Checking segments 12.0%"


class TestDelete:
    async def test_deletes_a_listed_archive(self, make_plugin, run_cycle):
        p = make_plugin(Borg, DELETE_CFG)
        _listed(p, run_cycle, "b2", "b1")
        plan = p.plan_action("delete_archive", archive="b1")
        assert "delete --stats" in plan.command and "::b1" in plan.command
        outcome = p.interpret_action("delete_archive", CmdResult(0, "5\n", ""))
        assert "for b1" in outcome.metadata['content']

    async def test_an_unlisted_archive_is_refused(self, make_plugin, run_cycle):
        p = make_plugin(Borg, DELETE_CFG)
        _listed(p, run_cycle, "b2")
        assert p.plan_action("delete_archive", archive="nope").success is False

    async def test_deleting_forgets_its_listings(self, make_plugin, run_cycle):
        p = make_plugin(Borg, DELETE_CFG)
        _listed(p, run_cycle, "b2", "b1")
        p.interpret_action("browse_archive", _framed(p, CmdResult(0, LISTING, "")), archive="b1", path="")
        p.plan_action("delete_archive", archive="b1")
        assert p.listing("b1", "") is None


class TestDiff:
    async def test_compares_with_the_previous_archive(self, make_plugin, run_cycle):
        p = make_plugin(Borg, BASE_CFG)
        _listed(p, run_cycle, "b3", "b2", "b1")
        cmd = p.plan_action("diff_archive", archive="b2").command.replace("'", "")
        assert "diff" in cmd and "::b1 b2" in cmd

    async def test_the_oldest_archive_has_nothing_to_compare(self, make_plugin, run_cycle):
        p = make_plugin(Borg, BASE_CFG)
        _listed(p, run_cycle, "b2", "b1")
        assert p.plan_action("diff_archive", archive="b1").success is False

    async def test_shows_the_changes(self, make_plugin, run_cycle):
        p = make_plugin(Borg, BASE_CFG)
        _listed(p, run_cycle, "b2", "b1")
        out = "added       1.20 kB etc/new\nremoved     0 B etc/old"
        content = p.interpret_action("diff_archive", _framed(p, CmdResult(0, out, "")), archive="b2").metadata['content']
        assert content.startswith("Changes from b1 to b2: 2") and "etc/new" in content
