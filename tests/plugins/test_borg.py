import json
import re
import time
from datetime import datetime
import pytest
from unittest.mock import AsyncMock

from vigil.plugins.borg import BorgCollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult, JobPlan
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
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(plugin_name: str, metric: str):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == plugin_name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S.000000")


def _list_json(epoch=None) -> str:
    archives = [] if epoch is None else [{"name": "host-2024", "start": _iso(epoch)}]
    return json.dumps({"repository": {"location": "/srv/repo"}, "archives": archives})


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(BorgCollectorPlugin, BASE_CFG)


def _collect(plugin, run_cycle, list_result, info_result=None):
    n = len(plugin.commands())
    if n <= 1:
        outputs = [list_result]
    else:
        outputs = [list_result, info_result if info_result is not None else CmdResult(0, "{}", "")]
    return run_cycle(plugin, lambda c, _it=iter(outputs): next(_it))


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
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "passphrase": "s3cret"})
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

    async def test_unparseable_output_is_failed(self, plugin, run_cycle):
        _collect(plugin, run_cycle, CmdResult(0, "not json", ""))
        assert _latest_status("test-borg") == "failed"

    async def test_missing_repo_config_is_failed(self, make_plugin, run_cycle):
        cfg = {k: v for k, v in BASE_CFG.items() if k != "repo"}
        p = make_plugin(BorgCollectorPlugin, cfg)
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
        p = make_plugin(BorgCollectorPlugin, cfg)
        assert p.max_age == 86400

    def test_command_queries_newest_archive_as_json(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, BASE_CFG)
        cmd = p._list_command()
        assert "borg list" in cmd
        assert "--last 10" in cmd
        assert "--json" in cmd
        assert "ssh://borg@host/srv/repo" in cmd

    def test_command_bypasses_lock(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, BASE_CFG)
        assert "--bypass-lock" in p._list_command()

    def test_command_sets_writable_borg_base_dir(self, make_plugin):
        cmd = make_plugin(BorgCollectorPlugin, BASE_CFG)._list_command()
        assert "BORG_BASE_DIR=" in cmd
        assert "$(mktemp -d)" in cmd

    def test_passphrase_passed_as_env_not_argv(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "passphrase": "s3cret"})
        cmd = p._list_command()
        assert cmd.startswith("BORG_PASSPHRASE=")
        assert cmd.index("BORG_PASSPHRASE=") < cmd.index("borg list")

    def test_passphrase_command_uses_passcommand(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "passphrase_command": "cat /run/secret"})
        cmd = p._list_command()
        assert "BORG_PASSCOMMAND=" in cmd
        assert "BORG_PASSPHRASE=" not in cmd

    def test_passphrase_file_inlined_as_passphrase(self, make_plugin, tmp_path):
        pf = tmp_path / "borg.pass"
        pf.write_text("s3cret-from-file\n")
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "passphrase_file": str(pf)})
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=s3cret-from-file" in cmd
        assert "BORG_PASSCOMMAND=" not in cmd
        assert str(pf) not in cmd

    def test_passphrase_beats_passphrase_file(self, make_plugin, tmp_path):
        pf = tmp_path / "borg.pass"
        pf.write_text("from-file")
        p = make_plugin(BorgCollectorPlugin, {
            **BASE_CFG, "passphrase": "inline-wins", "passphrase_file": str(pf),
        })
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=inline-wins" in cmd
        assert "from-file" not in cmd

    def test_missing_passphrase_file_omits_env(self, make_plugin, tmp_path):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "passphrase_file": str(tmp_path / "nope")})
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=" not in cmd
        assert "BORG_PASSCOMMAND=" not in cmd

    def test_no_passphrase_omits_env(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, BASE_CFG)
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=" not in cmd
        assert "BORG_PASSCOMMAND=" not in cmd

    def test_list_archives_configurable(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "list_archives": 3})
        assert "--last 3" in p._list_command()

    def test_list_archives_clamped_to_at_least_one(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "list_archives": 0})
        assert "--last 1" in p._list_command()

    def test_ssh_key_sets_borg_rsh(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "ssh_key": "/run/secrets/vigil_ssh_key"})
        cmd = p._list_command()
        assert "BORG_RSH=" in cmd
        assert "/run/secrets/vigil_ssh_key" in cmd
        assert "IdentitiesOnly=yes" in cmd
        assert "BatchMode=yes" in cmd

    def test_ssh_key_applies_to_backup_too(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {
            **BASE_CFG, "source_paths": ["/home"], "ssh_key": "/run/secrets/k",
        })
        assert "BORG_RSH=" in p._backup_command()

    def test_rsh_overrides_ssh_key(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {
            **BASE_CFG, "ssh_key": "/run/secrets/k", "rsh": "ssh -J jump.host",
        })
        cmd = p._list_command()
        assert "ssh -J jump.host" in cmd
        assert "/run/secrets/k" not in cmd

    def test_no_borg_rsh_without_key(self, make_plugin):
        assert "BORG_RSH=" not in make_plugin(BorgCollectorPlugin, BASE_CFG)._list_command()

    def test_borg_defaults_to_a_longer_timeout(self, make_plugin):
        from vigil.core.connectors.ssh_runner import TIMEOUT
        p = make_plugin(BorgCollectorPlugin, BASE_CFG)
        assert p.timeout == BorgCollectorPlugin.DEFAULT_TIMEOUT
        assert p.timeout > TIMEOUT

    def test_borg_timeout_is_overridable(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "timeout": "10m"})
        assert p.timeout == 600

    def test_no_sudo_by_default(self, make_plugin):
        assert "sudo" not in make_plugin(BorgCollectorPlugin, BASE_CFG)._list_command()

    def test_require_sudo_prefixes_command(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "require_sudo": True})
        cmd = p._list_command()
        assert cmd.startswith("sudo -n ")

    def test_require_sudo_keeps_env_after_sudo(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "require_sudo": True, "passphrase": "s3cret"})
        cmd = p._list_command()
        assert cmd.index("sudo") < cmd.index("BORG_PASSPHRASE=") < cmd.index("borg list")

    def test_local_path_repo_supported(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "repo": "/mnt/backups/repo"})
        assert "/mnt/backups/repo" in p._list_command()

    def test_custom_borg_bin_and_lock_wait(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "borg_bin": "/opt/borg", "lock_wait": 30})
        cmd = p._list_command()
        assert "/opt/borg list" in cmd
        assert "--lock-wait 30" in cmd


class TestActions:
    async def test_no_actions_without_source_paths(self, plugin):
        assert plugin.get_actions() == []

    async def test_backup_actions_exposed_with_source_paths(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "source_paths": ["/home"]})
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
        cmd = make_plugin(BorgCollectorPlugin, BACKUP_CFG)._backup_command()
        assert "borg create" in cmd
        assert "/home" in cmd and "/etc" in cmd
        assert "ssh://borg@host/srv/repo::" in cmd

    def test_archive_name_uses_prefix_and_is_sortable(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "archive_prefix": "nightly"})
        name = p.default_archive_name()
        assert name.startswith("nightly-")
        assert re.match(r"nightly-\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", name)

    def test_archive_prefix_defaults_to_monitor_name(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, BACKUP_CFG)
        assert p.default_archive_name().startswith("test-borg-")

    def test_excludes_are_passed(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "exclude": ["/home/*/.cache", "*.tmp"]})
        cmd = p._backup_command()
        assert cmd.count("--exclude ") >= 2
        assert "/home/*/.cache" in cmd
        assert "*.tmp" in cmd

    def test_exclude_accepts_bare_string(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "exclude": "/tmp"})
        assert p.exclude == ["/tmp"]
        assert "/tmp" in p._backup_command()

    def test_source_paths_accepts_bare_string(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "source_paths": "/home"})
        assert p.source_paths == ["/home"]

    def test_exclude_from_file_passed(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "exclude_from": "/etc/borg.excludes"})
        assert "--exclude-from /etc/borg.excludes" in p._backup_command()

    def test_exclude_if_present_markers(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "exclude_if_present": [".nobackup"]})
        assert "--exclude-if-present .nobackup" in p._backup_command()

    def test_one_file_system_on_by_default(self, make_plugin):
        assert "--one-file-system" in make_plugin(BorgCollectorPlugin, BACKUP_CFG)._backup_command()

    def test_one_file_system_can_be_disabled(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "one_file_system": False})
        assert "--one-file-system" not in p._backup_command()

    def test_exclude_caches_on_by_default(self, make_plugin):
        assert "--exclude-caches" in make_plugin(BorgCollectorPlugin, BACKUP_CFG)._backup_command()

    def test_compression_configurable(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "compression": "zstd,10"})
        assert "--compression zstd,10" in p._backup_command()

    def test_default_compression_is_zstd(self, make_plugin):
        assert "--compression zstd" in make_plugin(BorgCollectorPlugin, BACKUP_CFG)._backup_command()

    def test_backup_uses_persistent_cache_dir(self, make_plugin):
        cmd = make_plugin(BorgCollectorPlugin, BACKUP_CFG)._backup_command()
        assert "BORG_BASE_DIR=/var/cache/vigil-borg" in cmd
        assert "mktemp" not in cmd

    def test_cache_dir_configurable(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "cache_dir": "/srv/borgcache"})
        assert "BORG_BASE_DIR=/srv/borgcache" in p._backup_command()

    def test_poll_still_uses_throwaway_base_dir(self, make_plugin):
        assert "$(mktemp -d)" in make_plugin(BorgCollectorPlugin, BACKUP_CFG)._list_command()

    def test_backup_uses_long_lock_wait(self, make_plugin):
        assert "--lock-wait 600" in make_plugin(BorgCollectorPlugin, BACKUP_CFG)._backup_command()

    def test_backup_lock_wait_configurable(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "backup_lock_wait": 30})
        assert "--lock-wait 30" in p._backup_command()

    def test_backup_emits_structured_progress(self, make_plugin):
        cmd = make_plugin(BorgCollectorPlugin, BACKUP_CFG)._backup_command()
        assert "--log-json" in cmd
        assert "--progress" in cmd

    def test_dry_run_omits_stats_and_adds_flag(self, make_plugin):
        cmd = make_plugin(BorgCollectorPlugin, BACKUP_CFG)._backup_command(dry_run=True)
        assert "--dry-run" in cmd
        assert "--stats" not in cmd

    def test_real_backup_includes_stats(self, make_plugin):
        cmd = make_plugin(BorgCollectorPlugin, BACKUP_CFG)._backup_command(dry_run=False)
        assert "--stats" in cmd
        assert "--dry-run" not in cmd

    def test_passphrase_inlined_for_backup(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "passphrase": "s3cret"})
        assert "BORG_PASSPHRASE=s3cret" in p._backup_command()

    def test_backup_honours_require_sudo(self, make_plugin):
        p = make_plugin(BorgCollectorPlugin, {**BACKUP_CFG, "require_sudo": True})
        assert p._backup_command().startswith("sudo -n ")


def _streaming(lines, status=0, error=""):
    async def run(command, on_line=None, timeout=None, should_cancel=None):
        for line in lines:
            if on_line:
                on_line("stdout", line)
        return status, error
    return run


@pytest.fixture
def backup_plugin(make_plugin):
    return make_plugin(BorgCollectorPlugin, BACKUP_CFG)


async def _run_backup(plugin, engine_dispatch_action, action_id="run_backup"):
    """Drives plan_action -> network.run_job_plan (with job_on_line) ->
    interpret_job, mirroring VigilEngine.dispatch_action's JobPlan branch."""
    plan = plugin.plan_action(action_id)
    if plan is None:
        return False
    if not isinstance(plan, JobPlan):
        plugin.storage.apply(plan)
        return plan.success
    on_line = plugin.job_on_line(action_id)
    _job_id, status = await plugin.network.run_job_plan(plan, on_line=on_line)
    outcome = plugin.interpret_job(action_id, status)
    if hasattr(outcome, "success"):
        plugin.storage.apply(outcome)
        return outcome.success
    return outcome


class TestBackupExecution:
    async def test_successful_backup_records_job(self, backup_plugin, db_manager):
        backup_plugin.network._job.ssh.execute_streaming = _streaming(["done"])
        assert await _run_backup(backup_plugin, None) is True

        jobs = backup_plugin.network.recent()
        assert len(jobs) == 1
        assert jobs[0]['kind'] == 'backup'
        assert jobs[0]['state'] == 'succeeded'

    async def test_dry_run_recorded_as_its_own_kind(self, backup_plugin):
        backup_plugin.network._job.ssh.execute_streaming = _streaming(["ok"])
        await _run_backup(backup_plugin, None, "dry_run_backup")
        assert backup_plugin.network.recent()[0]['kind'] == 'dry-run'

    async def test_borg_warning_exit_is_still_success(self, backup_plugin):
        backup_plugin.network._job.ssh.execute_streaming = _streaming(["warn"], status=1)
        assert await _run_backup(backup_plugin, None) is True

    async def test_borg_error_exit_is_failure(self, backup_plugin):
        backup_plugin.network._job.ssh.execute_streaming = _streaming(["err"], status=2)
        assert await _run_backup(backup_plugin, None) is False

    async def test_job_command_is_redacted(self, backup_plugin, db_manager):
        p = backup_plugin
        p.passphrase = "s3cret"
        p.network._job.ssh.execute_streaming = _streaming(["ok"])
        await _run_backup(p, None)

        stored = p.network.recent()[0]['command']
        assert "s3cret" not in stored
        assert "BORG_PASSPHRASE=*****" in stored

    async def test_progress_is_parsed_from_log_json(self, backup_plugin, db_manager):
        progress = json.dumps({
            "type": "archive_progress",
            "path": "/home/user/file.txt",
            "original_size": 1048576,
            "deduplicated_size": 524288,
            "nfiles": 42,
        })
        seen = []

        async def streaming(command, on_line=None, timeout=None, should_cancel=None):
            on_line("stdout", progress)
            job_id = backup_plugin.network.current_job_id()
            seen.append(db_manager.get_job(job_id)['progress'])
            return 0, ""

        backup_plugin.network._job.ssh.execute_streaming = streaming
        await _run_backup(backup_plugin, None)

        assert "42 files" in seen[0]
        assert "/home/user/file.txt" in seen[0]

    def test_zero_counter_progress_is_ignored(self, backup_plugin, db_manager):
        job_id = db_manager.create_job("test-borg", "h", "backup", "cmd")
        real = json.dumps({
            "original_size": 1048576, "deduplicated_size": 524288,
            "nfiles": 42, "path": "src/big.bin",
            "type": "archive_progress", "finished": False,
        })
        opening = json.dumps({
            "original_size": 0, "deduplicated_size": 0, "nfiles": 0,
            "path": "src", "type": "archive_progress", "finished": False,
        })
        closing = json.dumps({"type": "archive_progress", "finished": True})

        backup_plugin._handle_backup_line(job_id, "stdout", real)
        backup_plugin._handle_backup_line(job_id, "stdout", closing)
        assert "42 files" in db_manager.get_job(job_id)['progress']

        backup_plugin._handle_backup_line(job_id, "stdout", opening)
        assert "42 files" in db_manager.get_job(job_id)['progress']

    async def test_borg_warnings_reach_the_event_log(self, backup_plugin, db_manager):
        message = json.dumps({
            "type": "log_message",
            "levelname": "WARNING",
            "message": "file changed while we backed it up",
        })
        backup_plugin.network._job.ssh.execute_streaming = _streaming([message])
        await _run_backup(backup_plugin, None)
        db_manager.flush()

        from vigil.core.database.database import Event
        with db.connection_context():
            events = [e.message for e in Event.select().where(Event.message.contains("file changed"))]
        assert events

    async def test_non_json_output_does_not_crash(self, backup_plugin):
        backup_plugin.network._job.ssh.execute_streaming = _streaming(
            ["Warning: Permanently added host", "{not json", "plain text"]
        )
        assert await _run_backup(backup_plugin, None) is True

    async def test_output_lines_are_stored(self, backup_plugin, db_manager):
        backup_plugin.network._job.ssh.execute_streaming = _streaming(["line1", "line2"])
        await _run_backup(backup_plugin, None)

        job_id = backup_plugin.network.recent()[0]['id']
        assert [o['message'] for o in db_manager.job_output(job_id)] == ["line1", "line2"]


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
        cmd = make_plugin(BorgCollectorPlugin, BASE_CFG)._info_command()
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
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "collect_stats": False})
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
            loglines = LogLine.select().where(LogLine.source == "test-borg").count()

        assert events > 0, "plugin wrote no events for the panel to show"
        assert loglines == 0, "plugin does not collect target logs; panel must read Events"


class TestArchiveCache:
    async def test_archives_cached_for_ui(self, make_plugin, run_cycle):
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "collect_stats": False})
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
        p = make_plugin(BorgCollectorPlugin, {**BASE_CFG, "list_archives": 5})
        assert "--last 5" in p._info_command()

    async def test_archives_without_stats_keep_names(self, plugin, run_cycle):
        now = int(time.time())
        info = json.dumps({"cache": {"stats": {"total_size": 10,
                                               "unique_csize": 5}}})
        _collect(plugin, run_cycle, CmdResult(0, _multi_json(now - 3600), ""), CmdResult(0, info, ""))

        archives, _ = plugin.cached_archives()
        assert [a["name"] for a in archives] == ["archive-0"]
        assert "original" not in archives[0]
