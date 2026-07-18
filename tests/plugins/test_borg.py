import json
import re
import time
from datetime import datetime
import pytest
from unittest.mock import AsyncMock

from vigil.plugins.borg import BorgPlugin
from vigil.core.data.database import db, StatusHistory, Metric


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
    # borg emits local-naive ISO timestamps.
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S.000000")


def _list_json(epoch=None) -> str:
    archives = [] if epoch is None else [{"name": "host-2024", "start": _iso(epoch)}]
    return json.dumps({"repository": {"location": "/srv/repo"}, "archives": archives})


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(BorgPlugin, BASE_CFG)


# ---------------------------------------------------------------------------
# Freshness logic
# ---------------------------------------------------------------------------

class TestFreshness:
    async def test_recent_archive_is_online(self, plugin):
        recent = int(time.time()) - 3600  # 1h ago, max_age 1d
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _list_json(recent), ""))
        await plugin.on_collect()
        assert _latest_status("test-borg") == "online"

    async def test_stale_archive_is_failed(self, plugin):
        stale = int(time.time()) - 3 * 24 * 3600  # 3d ago, max_age 1d
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _list_json(stale), ""))
        await plugin.on_collect()
        assert _latest_status("test-borg") == "failed"

    async def test_no_archives_is_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _list_json(None), ""))
        await plugin.on_collect()
        assert _latest_status("test-borg") == "failed"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _captured(plugin) -> list:
    """Record every db_logger.write message for assertions."""
    written = []
    plugin.db_logger.write = lambda msg, level="INFO": written.append((level, msg))
    return written


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
    async def test_logs_each_archive(self, plugin):
        now = int(time.time())
        out = _multi_json(now - 3600, now - 90000, now - 180000)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, out, ""))
        log = _captured(plugin)
        await plugin.on_collect()
        messages = " | ".join(m for _, m in log)
        for name in ("archive-0", "archive-1", "archive-2"):
            assert name in messages

    async def test_logs_repository_metadata(self, plugin):
        now = int(time.time())
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _multi_json(now - 3600), "")
        )
        log = _captured(plugin)
        await plugin.on_collect()
        messages = " | ".join(m for _, m in log)
        assert "/srv/repo" in messages
        assert "repokey-blake2" in messages

    async def test_archives_logged_newest_first(self, plugin):
        now = int(time.time())
        # Deliberately out of order — the log must still read newest first.
        out = _multi_json(now - 180000, now - 3600, now - 90000)
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, out, ""))
        log = _captured(plugin)
        await plugin.on_collect()
        names = [m.strip().split(" ")[0] for _, m in log if m.startswith("  archive-")]
        assert names == ["archive-1", "archive-2", "archive-0"]

    async def test_logs_command_with_passphrase_redacted(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "passphrase": "s3cret"})
        now = int(time.time())
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _multi_json(now - 3600), "")
        )
        log = _captured(p)
        await p.on_collect()
        messages = " | ".join(m for _, m in log)
        # The command is logged, but never the secret.
        assert "borg list" in messages
        assert "BORG_PASSPHRASE=*****" in messages
        assert "s3cret" not in messages

    async def test_failure_logs_exit_code_and_hint(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(1, "", "sudo: borg: command not found")
        )
        log = _captured(plugin)
        await plugin.on_collect()
        messages = " | ".join(m for _, m in log)
        assert "exit 1" in messages
        assert "not on PATH" in messages

    async def test_publickey_failure_hints_at_ssh_key(self, plugin):
        # An ssh:// repo rejecting borg's own hop says "Permission denied
        # (publickey)". The generic permission-denied hint would advise
        # `require_sudo`, which is useless when sudo is already on — the real
        # fault is the identity borg offered to the repo server.
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(
            2, "", "Remote: borg@heimdall.technet: Permission denied (publickey)."
        ))
        log = _captured(plugin)
        await plugin.on_collect()
        messages = " | ".join(m for _, m in log)
        assert "ssh_key" in messages
        assert "require_sudo" not in messages

    async def test_permission_denied_hint(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(2, "", "Permission denied: '/srv/repo/config'")
        )
        log = _captured(plugin)
        await plugin.on_collect()
        assert any("require_sudo" in m for _, m in log)

    async def test_unparseable_output_logs_raw_snippet(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, "Warning: something odd", "")
        )
        log = _captured(plugin)
        await plugin.on_collect()
        assert any("Warning: something odd" in m for _, m in log)


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestFailures:
    async def test_borg_error_is_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(2, "", "Repository is locked")
        )
        await plugin.on_collect()
        assert _latest_status("test-borg") == "failed"

    async def test_unparseable_output_is_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, "not json", ""))
        await plugin.on_collect()
        assert _latest_status("test-borg") == "failed"

    async def test_missing_repo_config_is_failed(self, make_plugin):
        cfg = {k: v for k, v in BASE_CFG.items() if k != "repo"}
        p = make_plugin(BorgPlugin, cfg)
        # No SSH call should be attempted with no repo configured.
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, _list_json(int(time.time())), ""))
        await p.on_collect()
        assert _latest_status("test-borg") == "failed"
        p.ssh_collector.fetch_output.assert_not_called()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    async def test_last_backup_epoch_recorded(self, plugin):
        epoch = int(time.time()) - 500
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _list_json(epoch), ""))
        await plugin.on_collect()
        assert abs(_latest_metric("test-borg", "last_backup_epoch") - epoch) <= 1

    async def test_archive_count_recorded(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _list_json(int(time.time())), "")
        )
        await plugin.on_collect()
        assert _latest_metric("test-borg", "archive_count") == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------

class TestCommand:
    def test_default_max_age_is_one_day(self, make_plugin):
        cfg = {k: v for k, v in BASE_CFG.items() if k != "max_age"}
        p = make_plugin(BorgPlugin, cfg)
        assert p.max_age == 86400

    def test_command_queries_newest_archive_as_json(self, make_plugin):
        p = make_plugin(BorgPlugin, BASE_CFG)
        cmd = p._list_command()
        assert "borg list" in cmd
        # Defaults to the 10 most recent so the log can show repo contents;
        # only the newest drives status.
        assert "--last 10" in cmd
        assert "--json" in cmd
        assert "ssh://borg@host/srv/repo" in cmd

    def test_command_bypasses_lock(self, make_plugin):
        # Read-only health check on a repo Vigil can read but not write (e.g.
        # a 0750 borg-group repo): borg's normal lock writes into the repo dir
        # and fails with EACCES, so the poll must skip locking entirely.
        p = make_plugin(BorgPlugin, BASE_CFG)
        assert "--bypass-lock" in p._list_command()

    def test_command_sets_writable_borg_base_dir(self, make_plugin):
        # Vigil often logs in as a system account with home /var/empty, where
        # borg dies creating ~/.config/borg. BORG_BASE_DIR must relocate its
        # dirs to a writable temp dir on the remote host.
        cmd = make_plugin(BorgPlugin, BASE_CFG)._list_command()
        assert "BORG_BASE_DIR=" in cmd
        # Must run the substitution on the remote shell (unquoted $(...)).
        assert "$(mktemp -d)" in cmd

    def test_passphrase_passed_as_env_not_argv(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "passphrase": "s3cret"})
        cmd = p._list_command()
        # Exported as an environment prefix, before the borg invocation.
        assert cmd.startswith("BORG_PASSPHRASE=")
        assert cmd.index("BORG_PASSPHRASE=") < cmd.index("borg list")

    def test_passphrase_command_uses_passcommand(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "passphrase_command": "cat /run/secret"})
        cmd = p._list_command()
        assert "BORG_PASSCOMMAND=" in cmd
        assert "BORG_PASSPHRASE=" not in cmd

    def test_passphrase_file_inlined_as_passphrase(self, make_plugin, tmp_path):
        # The file is read on the Vigil host and its contents inlined as
        # BORG_PASSPHRASE, so the remote host needs no copy of the secret.
        pf = tmp_path / "borg.pass"
        pf.write_text("s3cret-from-file\n")  # trailing newline must be stripped
        p = make_plugin(BorgPlugin, {**BASE_CFG, "passphrase_file": str(pf)})
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=s3cret-from-file" in cmd
        assert "BORG_PASSCOMMAND=" not in cmd
        # Never passed as a passcommand path — the secret value is inlined.
        assert str(pf) not in cmd

    def test_passphrase_beats_passphrase_file(self, make_plugin, tmp_path):
        pf = tmp_path / "borg.pass"
        pf.write_text("from-file")
        p = make_plugin(BorgPlugin, {
            **BASE_CFG, "passphrase": "inline-wins", "passphrase_file": str(pf),
        })
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=inline-wins" in cmd
        assert "from-file" not in cmd

    def test_missing_passphrase_file_omits_env(self, make_plugin, tmp_path):
        # An unreadable file must not crash command building; it logs and falls
        # through to no passphrase (borg then fails clearly on the encrypted repo).
        p = make_plugin(BorgPlugin, {**BASE_CFG, "passphrase_file": str(tmp_path / "nope")})
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=" not in cmd
        assert "BORG_PASSCOMMAND=" not in cmd

    def test_no_passphrase_omits_env(self, make_plugin):
        p = make_plugin(BorgPlugin, BASE_CFG)
        cmd = p._list_command()
        assert "BORG_PASSPHRASE=" not in cmd
        assert "BORG_PASSCOMMAND=" not in cmd

    def test_list_archives_configurable(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "list_archives": 3})
        assert "--last 3" in p._list_command()

    def test_list_archives_clamped_to_at_least_one(self, make_plugin):
        # --last 0 makes borg dump every archive in the repo.
        p = make_plugin(BorgPlugin, {**BASE_CFG, "list_archives": 0})
        assert "--last 1" in p._list_command()

    def test_ssh_key_sets_borg_rsh(self, make_plugin):
        # borg opens its own connection to an ssh:// repo; without an explicit
        # identity it offers the invoking user's default keys (root's, under
        # sudo), which the borg server usually does not authorize.
        p = make_plugin(BorgPlugin, {**BASE_CFG, "ssh_key": "/run/secrets/vigil_ssh_key"})
        cmd = p._list_command()
        assert "BORG_RSH=" in cmd
        assert "/run/secrets/vigil_ssh_key" in cmd
        # The key must be used as given, and never prompt.
        assert "IdentitiesOnly=yes" in cmd
        assert "BatchMode=yes" in cmd

    def test_ssh_key_applies_to_backup_too(self, make_plugin):
        p = make_plugin(BorgPlugin, {
            **BASE_CFG, "source_paths": ["/home"], "ssh_key": "/run/secrets/k",
        })
        assert "BORG_RSH=" in p._backup_command()

    def test_rsh_overrides_ssh_key(self, make_plugin):
        p = make_plugin(BorgPlugin, {
            **BASE_CFG, "ssh_key": "/run/secrets/k", "rsh": "ssh -J jump.host",
        })
        cmd = p._list_command()
        assert "ssh -J jump.host" in cmd
        assert "/run/secrets/k" not in cmd

    def test_no_borg_rsh_without_key(self, make_plugin):
        # A local-path repo needs no onward SSH at all.
        assert "BORG_RSH=" not in make_plugin(BorgPlugin, BASE_CFG)._list_command()

    def test_borg_defaults_to_a_longer_timeout(self, make_plugin):
        # An ssh:// repo adds a second SSH hop and a busy repo answers slowly;
        # the framework default (tuned for quick reads) times these out.
        from vigil.core.modules.collectors.ssh_collector import TIMEOUT
        p = make_plugin(BorgPlugin, BASE_CFG)
        assert p.timeout == BorgPlugin.DEFAULT_TIMEOUT
        assert p.timeout > TIMEOUT

    def test_borg_timeout_is_overridable(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "timeout": "10m"})
        assert p.timeout == 600

    def test_no_sudo_by_default(self, make_plugin):
        assert "sudo" not in make_plugin(BorgPlugin, BASE_CFG)._list_command()

    def test_require_sudo_prefixes_command(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "require_sudo": True})
        cmd = p._list_command()
        # -n so a missing NOPASSWD rule errors out instead of hanging the poll
        # on a password prompt.
        assert cmd.startswith("sudo -n ")

    def test_require_sudo_keeps_env_after_sudo(self, make_plugin):
        # sudo scrubs the environment it inherits, so a leading
        # `BORG_PASSPHRASE=... sudo borg` would drop the passphrase. The
        # assignments must be sudo's own VAR=value arguments.
        p = make_plugin(BorgPlugin, {**BASE_CFG, "require_sudo": True, "passphrase": "s3cret"})
        cmd = p._list_command()
        assert cmd.index("sudo") < cmd.index("BORG_PASSPHRASE=") < cmd.index("borg list")

    def test_local_path_repo_supported(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "repo": "/mnt/backups/repo"})
        assert "/mnt/backups/repo" in p._list_command()

    def test_custom_borg_bin_and_lock_wait(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "borg_bin": "/opt/borg", "lock_wait": 30})
        cmd = p._list_command()
        assert "/opt/borg list" in cmd
        assert "--lock-wait 30" in cmd


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class TestActions:
    async def test_no_actions_without_source_paths(self, plugin):
        # Without something to back up, the button could only ever fail.
        assert plugin.get_actions() == []

    async def test_backup_actions_exposed_with_source_paths(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "source_paths": ["/home"]})
        ids = {a['action_id'] for a in p.get_actions()}
        assert ids == {"run_backup", "dry_run_backup"}

    async def test_unknown_action_returns_false(self, plugin):
        assert await plugin.on_action("nonsense") is False

    async def test_backup_without_source_paths_fails(self, plugin):
        log = _captured(plugin)
        assert await plugin.on_action("run_backup") is False
        assert any("source_paths" in m for _, m in log)


# ---------------------------------------------------------------------------
# Backup command construction
# ---------------------------------------------------------------------------

BACKUP_CFG = {**BASE_CFG, "source_paths": ["/home", "/etc"]}


class TestBackupCommand:
    def test_includes_sources_and_repo_archive(self, make_plugin):
        cmd = make_plugin(BorgPlugin, BACKUP_CFG)._backup_command()
        assert "borg create" in cmd
        assert "/home" in cmd and "/etc" in cmd
        # Archive target is repo::name
        assert "ssh://borg@host/srv/repo::" in cmd

    def test_archive_name_uses_prefix_and_is_sortable(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "archive_prefix": "nightly"})
        name = p.default_archive_name()
        assert name.startswith("nightly-")
        # UTC ISO-ish stamp so names sort chronologically across DST changes.
        assert re.match(r"nightly-\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", name)

    def test_archive_prefix_defaults_to_monitor_name(self, make_plugin):
        p = make_plugin(BorgPlugin, BACKUP_CFG)
        assert p.default_archive_name().startswith("test-borg-")

    def test_excludes_are_passed(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "exclude": ["/home/*/.cache", "*.tmp"]})
        cmd = p._backup_command()
        assert cmd.count("--exclude ") >= 2
        assert "/home/*/.cache" in cmd
        assert "*.tmp" in cmd

    def test_exclude_accepts_bare_string(self, make_plugin):
        # YAML makes `exclude: "/tmp"` natural; it must not be iterated
        # character by character into four bogus patterns.
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "exclude": "/tmp"})
        assert p.exclude == ["/tmp"]
        assert "/tmp" in p._backup_command()

    def test_source_paths_accepts_bare_string(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "source_paths": "/home"})
        assert p.source_paths == ["/home"]

    def test_exclude_from_file_passed(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "exclude_from": "/etc/borg.excludes"})
        assert "--exclude-from /etc/borg.excludes" in p._backup_command()

    def test_exclude_if_present_markers(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "exclude_if_present": [".nobackup"]})
        assert "--exclude-if-present .nobackup" in p._backup_command()

    def test_one_file_system_on_by_default(self, make_plugin):
        # Without it, a source of "/" silently pulls in every mount.
        assert "--one-file-system" in make_plugin(BorgPlugin, BACKUP_CFG)._backup_command()

    def test_one_file_system_can_be_disabled(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "one_file_system": False})
        assert "--one-file-system" not in p._backup_command()

    def test_exclude_caches_on_by_default(self, make_plugin):
        assert "--exclude-caches" in make_plugin(BorgPlugin, BACKUP_CFG)._backup_command()

    def test_compression_configurable(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "compression": "zstd,10"})
        assert "--compression zstd,10" in p._backup_command()

    def test_default_compression_is_zstd(self, make_plugin):
        assert "--compression zstd" in make_plugin(BorgPlugin, BACKUP_CFG)._backup_command()

    def test_backup_uses_persistent_cache_dir(self, make_plugin):
        # A throwaway BORG_BASE_DIR would force a full chunk-cache rebuild,
        # turning every incremental backup into a full re-read.
        cmd = make_plugin(BorgPlugin, BACKUP_CFG)._backup_command()
        assert "BORG_BASE_DIR=/var/cache/vigil-borg" in cmd
        assert "mktemp" not in cmd

    def test_cache_dir_configurable(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "cache_dir": "/srv/borgcache"})
        assert "BORG_BASE_DIR=/srv/borgcache" in p._backup_command()

    def test_poll_still_uses_throwaway_base_dir(self, make_plugin):
        # Read-only polls keep the temp dir: they need no cache and the account
        # may have no writable home.
        assert "$(mktemp -d)" in make_plugin(BorgPlugin, BACKUP_CFG)._list_command()

    def test_backup_uses_long_lock_wait(self, make_plugin):
        # A backup should queue behind a concurrent operation, not give up
        # after the short poll timeout.
        assert "--lock-wait 600" in make_plugin(BorgPlugin, BACKUP_CFG)._backup_command()

    def test_backup_lock_wait_configurable(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "backup_lock_wait": 30})
        assert "--lock-wait 30" in p._backup_command()

    def test_backup_emits_structured_progress(self, make_plugin):
        cmd = make_plugin(BorgPlugin, BACKUP_CFG)._backup_command()
        assert "--log-json" in cmd
        assert "--progress" in cmd

    def test_dry_run_omits_stats_and_adds_flag(self, make_plugin):
        # --stats is rejected by borg alongside --dry-run.
        cmd = make_plugin(BorgPlugin, BACKUP_CFG)._backup_command(dry_run=True)
        assert "--dry-run" in cmd
        assert "--stats" not in cmd

    def test_real_backup_includes_stats(self, make_plugin):
        cmd = make_plugin(BorgPlugin, BACKUP_CFG)._backup_command(dry_run=False)
        assert "--stats" in cmd
        assert "--dry-run" not in cmd

    def test_passphrase_inlined_for_backup(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "passphrase": "s3cret"})
        assert "BORG_PASSPHRASE=s3cret" in p._backup_command()

    def test_backup_honours_require_sudo(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BACKUP_CFG, "require_sudo": True})
        assert p._backup_command().startswith("sudo -n ")


# ---------------------------------------------------------------------------
# Backup execution
# ---------------------------------------------------------------------------

def _streaming(lines, status=0, error=""):
    """Fake execute_streaming emitting `lines` then exiting with `status`."""
    def run(command, on_line=None, timeout=None, should_cancel=None):
        for line in lines:
            if on_line:
                on_line("stdout", line)
        return status, error
    return run


@pytest.fixture
def backup_plugin(make_plugin):
    return make_plugin(BorgPlugin, BACKUP_CFG)


class TestBackupExecution:
    async def test_successful_backup_records_job(self, backup_plugin, db_manager):
        backup_plugin.job_controller.ssh.execute_streaming = _streaming(["done"])
        assert await backup_plugin.on_action("run_backup") is True

        jobs = backup_plugin.job_controller.recent()
        assert len(jobs) == 1
        assert jobs[0]['kind'] == 'backup'
        assert jobs[0]['state'] == 'succeeded'

    async def test_dry_run_recorded_as_its_own_kind(self, backup_plugin):
        backup_plugin.job_controller.ssh.execute_streaming = _streaming(["ok"])
        await backup_plugin.on_action("dry_run_backup")
        assert backup_plugin.job_controller.recent()[0]['kind'] == 'dry-run'

    async def test_borg_warning_exit_is_still_success(self, backup_plugin):
        # borg exits 1 when e.g. a file vanished mid-backup; the archive is
        # still valid, so this must not be reported as a failure.
        backup_plugin.job_controller.ssh.execute_streaming = _streaming(["warn"], status=1)
        log = _captured(backup_plugin)
        assert await backup_plugin.on_action("run_backup") is True
        assert any("warning" in m.lower() for _, m in log)

    async def test_borg_error_exit_is_failure(self, backup_plugin):
        backup_plugin.job_controller.ssh.execute_streaming = _streaming(["err"], status=2)
        assert await backup_plugin.on_action("run_backup") is False

    async def test_job_command_is_redacted(self, backup_plugin, db_manager):
        p = backup_plugin
        p.passphrase = "s3cret"
        p.job_controller.ssh.execute_streaming = _streaming(["ok"])
        await p.on_action("run_backup")

        stored = p.job_controller.recent()[0]['command']
        assert "s3cret" not in stored
        assert "BORG_PASSPHRASE=*****" in stored

    async def test_progress_is_parsed_from_log_json(self, backup_plugin, db_manager):
        # Progress is observed while the job runs; once it completes the panel
        # switches to a final summary (see test_completion_sets_a_final_summary).
        progress = json.dumps({
            "type": "archive_progress",
            "path": "/home/user/file.txt",
            "original_size": 1048576,
            "deduplicated_size": 524288,
            "nfiles": 42,
        })
        seen = []

        def streaming(command, on_line=None, timeout=None, should_cancel=None):
            on_line("stdout", progress)
            job_id = backup_plugin.job_controller.current_job_id()
            seen.append(db_manager.get_job(job_id)['progress'])
            return 0, ""

        backup_plugin.job_controller.ssh.execute_streaming = streaming
        await backup_plugin.on_action("run_backup")

        assert "42 files" in seen[0]
        assert "/home/user/file.txt" in seen[0]

    def test_zero_counter_progress_is_ignored(self, backup_plugin, db_manager):
        # borg brackets a run with counter-less progress records: an opening one
        # before anything is read, and a bare {"finished": true} at the end.
        # Persisting either leaves a finished backup reading "0 files, 0 B read".
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

        # An opening record must not overwrite real totals either.
        backup_plugin._handle_backup_line(job_id, "stdout", opening)
        assert "42 files" in db_manager.get_job(job_id)['progress']

    async def test_completion_sets_a_final_summary(self, backup_plugin, db_manager):
        # A backup can finish before any counter-bearing record arrives; the
        # panel must still end on something meaningful rather than blank.
        backup_plugin.job_controller.ssh.execute_streaming = _streaming(
            [json.dumps({"type": "archive_progress", "finished": True})]
        )
        await backup_plugin.on_action("run_backup")

        job_id = backup_plugin.job_controller.recent()[0]['id']
        assert db_manager.get_job(job_id)['progress'] == "Backup completed"

    async def test_failed_job_summary_reports_exit_code(self, backup_plugin, db_manager):
        backup_plugin.job_controller.ssh.execute_streaming = _streaming(["x"], status=2)
        await backup_plugin.on_action("run_backup")

        job_id = backup_plugin.job_controller.recent()[0]['id']
        assert "exit 2" in db_manager.get_job(job_id)['progress']

    async def test_borg_warnings_reach_the_event_log(self, backup_plugin):
        message = json.dumps({
            "type": "log_message",
            "levelname": "WARNING",
            "message": "file changed while we backed it up",
        })
        backup_plugin.job_controller.ssh.execute_streaming = _streaming([message])
        log = _captured(backup_plugin)
        await backup_plugin.on_action("run_backup")
        assert any("file changed" in m for _, m in log)

    async def test_non_json_output_does_not_crash(self, backup_plugin):
        # ssh banners and tracebacks appear alongside --log-json records.
        backup_plugin.job_controller.ssh.execute_streaming = _streaming(
            ["Warning: Permanently added host", "{not json", "plain text"]
        )
        assert await backup_plugin.on_action("run_backup") is True

    async def test_output_lines_are_stored(self, backup_plugin, db_manager):
        backup_plugin.job_controller.ssh.execute_streaming = _streaming(["line1", "line2"])
        await backup_plugin.on_action("run_backup")

        job_id = backup_plugin.job_controller.recent()[0]['id']
        assert [o['message'] for o in db_manager.job_output(job_id)] == ["line1", "line2"]


# ---------------------------------------------------------------------------
# Repository statistics (borg info)
# ---------------------------------------------------------------------------

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
        cmd = make_plugin(BorgPlugin, BASE_CFG)._info_command()
        assert "borg info" in cmd
        assert "--json" in cmd

    async def test_stats_recorded_as_metrics(self, plugin):
        now = int(time.time())
        plugin.ssh_collector.fetch_output = AsyncMock(
            side_effect=[(0, _list_json(now - 60), ""), (0, _info_json(), "")]
        )
        await plugin.on_collect()

        assert _latest_metric("test-borg", "original_size") == pytest.approx(1000)
        assert _latest_metric("test-borg", "deduplicated_size") == pytest.approx(250)

    async def test_dedup_ratio_is_derived(self, plugin):
        now = int(time.time())
        plugin.ssh_collector.fetch_output = AsyncMock(
            side_effect=[(0, _list_json(now - 60), ""), (0, _info_json(total=1000, unique=250), "")]
        )
        await plugin.on_collect()
        assert _latest_metric("test-borg", "dedup_ratio") == pytest.approx(4.0)

    async def test_stats_disabled_skips_info_call(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "collect_stats": False})
        now = int(time.time())
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, _list_json(now - 60), ""))
        await p.on_collect()
        # Only the list call — no second round trip.
        assert p.ssh_collector.fetch_output.call_count == 1

    async def test_info_failure_does_not_fail_the_monitor(self, plugin):
        # Freshness was already established by the list call; losing a size
        # datapoint must not turn a healthy monitor red.
        now = int(time.time())
        plugin.ssh_collector.fetch_output = AsyncMock(
            side_effect=[(0, _list_json(now - 60), ""), (2, "", "info exploded")]
        )
        await plugin.on_collect()
        assert _latest_status("test-borg") == "online"

    async def test_unparseable_info_is_tolerated(self, plugin):
        now = int(time.time())
        plugin.ssh_collector.fetch_output = AsyncMock(
            side_effect=[(0, _list_json(now - 60), ""), (0, "not json", "")]
        )
        await plugin.on_collect()
        assert _latest_status("test-borg") == "online"

    async def test_empty_repo_skips_the_stats_call(self, plugin):
        # An empty repo has no size stats worth a second round trip.
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _list_json(None), ""))
        await plugin.on_collect()
        assert plugin.ssh_collector.fetch_output.call_count == 1
        assert _latest_status("test-borg") == "failed"

    async def test_status_is_set_before_stats_are_fetched(self, plugin):
        # The status decision is the monitor's purpose; it must not wait on the
        # optional stats round trip, which may be slow or hang.
        seen = {}

        async def fetch(command):
            if "borg info" in command:
                seen['status_at_info_time'] = _latest_status("test-borg")
                return (0, _info_json(), "")
            return (0, _list_json(int(time.time()) - 60), "")

        plugin.ssh_collector.fetch_output = AsyncMock(side_effect=fetch)
        await plugin.on_collect()
        assert seen['status_at_info_time'] == "online"

    async def test_missing_cache_stats_yields_no_metrics(self, plugin):
        # Older borg omits the stats block when the cache has not been built.
        now = int(time.time())
        plugin.ssh_collector.fetch_output = AsyncMock(
            side_effect=[(0, _list_json(now - 60), ""), (0, json.dumps({"repository": {}}), "")]
        )
        await plugin.on_collect()
        assert _latest_metric("test-borg", "original_size") is None


# ---------------------------------------------------------------------------
# Archive caching for the UI
# ---------------------------------------------------------------------------

class TestArchiveCache:
    async def test_archives_cached_for_ui(self, make_plugin):
        p = make_plugin(BorgPlugin, {**BASE_CFG, "collect_stats": False})
        now = int(time.time())
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _multi_json(now - 3600, now - 7200), "")
        )
        await p.on_collect()

        archives, info = p.cached_archives()
        assert [a['name'] for a in archives] == ["archive-0", "archive-1"]
        assert info['location'] == "/srv/repo"

    async def test_cached_archives_empty_before_first_poll(self, plugin):
        assert plugin.cached_archives() == ([], {})

    async def test_archive_sizes_merged_from_info(self, plugin):
        # `borg list --json` carries no size fields at all; the per-archive
        # stats come from `borg info --json --last N` and must be folded into
        # the cached list the table reads.
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
        plugin.ssh_collector.fetch_output = AsyncMock(
            side_effect=[(0, _multi_json(now - 3600), ""), (0, info, "")]
        )
        await plugin.on_collect()

        archives, _ = plugin.cached_archives()
        first = next(a for a in archives if a["name"] == "archive-0")
        assert first["original"] == pytest.approx(400000)
        assert first["deduplicated"] == pytest.approx(1024)
        assert first["nfiles"] == pytest.approx(42)

    async def test_info_command_requests_per_archive_stats(self, make_plugin):
        # Sizes only appear per archive when --last is passed to info.
        p = make_plugin(BorgPlugin, {**BASE_CFG, "list_archives": 5})
        assert "--last 5" in p._info_command()

    async def test_archives_without_stats_keep_names(self, plugin):
        # An info call that returns no per-archive stats must leave the cached
        # list intact, so the table still shows names and ages.
        now = int(time.time())
        info = json.dumps({"cache": {"stats": {"total_size": 10,
                                               "unique_csize": 5}}})
        plugin.ssh_collector.fetch_output = AsyncMock(
            side_effect=[(0, _multi_json(now - 3600), ""), (0, info, "")]
        )
        await plugin.on_collect()

        archives, _ = plugin.cached_archives()
        assert [a["name"] for a in archives] == ["archive-0"]
        assert "original" not in archives[0]
