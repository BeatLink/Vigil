import json
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
    async def test_no_actions_exposed(self, plugin):
        assert plugin.get_actions() == []

    async def test_unknown_action_returns_false(self, plugin):
        assert await plugin.on_action("run_backup") is False
