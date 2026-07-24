import time
import pytest
from unittest.mock import AsyncMock
from vigil.plugins.systemd_service import SystemdCollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric, LogLine, flush_writes

pytestmark = pytest.mark.asyncio


CONTINUOUS_CFG = {
    "name":         "test-nginx",
    "id":           "test-nginx",
    "service_name": "nginx.service",
    "interval":     60,
    "ssh_config":   {"host": "test.host"},
}

ONESHOT_CFG = {
    "name":         "test-upgrade",
    "id":           "test-upgrade",
    "service_name": "nixos-upgrade.service",
    "interval":     3600,
    "max_age":      "1w",
    "ssh_config":   {"host": "test.host"},
}


def _latest_status(plugin_id: str) -> str | None:
    flush_writes()
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(plugin_name: str, metric: str) -> float | None:
    flush_writes()
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == plugin_name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _oneshot_output(result="success", exit_code="0", epoch=None,
                    active="inactive", sub="dead") -> str:
    if epoch is None:
        epoch = int(time.time()) - 3600
    return f"result={result} exit={exit_code} epoch={epoch} active={active} sub={sub}"


def _run(plugin, outputs):
    """outputs: list of (exit_code, stdout, stderr) tuples matching plugin.commands() order."""
    results = [CmdResult(*o) for o in outputs]
    result = plugin.parse(results)
    plugin.storage.apply(result)
    return result


class TestContinuousMode:
    @pytest.fixture
    def plugin(self, make_plugin):
        return make_plugin(SystemdCollectorPlugin, CONTINUOUS_CFG)

    async def test_active_service_is_online(self, plugin):
        _run(plugin, [(0, "active", ""), (0, "log line", "")])
        assert _latest_status("test-nginx") == "online"

    async def test_inactive_service_is_warning(self, plugin):
        _run(plugin, [(1, "inactive", ""), (0, "", "")])
        assert _latest_status("test-nginx") == "warning"

    async def test_active_metric_recorded(self, plugin):
        _run(plugin, [(0, "active", ""), (0, "", "")])
        assert _latest_metric("test-nginx", "active") == pytest.approx(1.0)

    async def test_inactive_metric_recorded(self, plugin):
        _run(plugin, [(1, "inactive", ""), (0, "", "")])
        assert _latest_metric("test-nginx", "active") == pytest.approx(0.0)

    async def test_journal_failure_sets_failed(self, plugin):
        _run(plugin, [(0, "active", ""), (-1, "", "journalctl: permission denied")])
        assert _latest_status("test-nginx") == "failed"

    async def test_journal_lines_persisted(self, plugin):
        _run(plugin, [(0, "active", ""), (0, "2024-05-01T12:00:00+0000 host nginx[1]: started", "")])
        flush_writes()
        with db.connection_context():
            rows = list(LogLine.select().where(LogLine.source == "test-nginx"))
        assert len(rows) == 1
        assert "started" in rows[0].message

    async def test_repeated_journal_line_deduplicated(self, plugin):
        line = "2024-05-01T12:00:00+0000 host nginx[1]: same message"
        for _ in range(2):
            _run(plugin, [(0, "active", ""), (0, line, "")])
        flush_writes()
        with db.connection_context():
            count = LogLine.select().where(LogLine.source == "test-nginx").count()
        assert count == 1

    async def test_error_line_classified_as_error(self, plugin):
        _run(plugin, [(0, "active", ""), (0, "2024-05-01T12:00:00+0000 host nginx[1]: FAILED to bind", "")])
        flush_writes()
        with db.connection_context():
            row = LogLine.select().where(LogLine.source == "test-nginx").first()
        assert row.level == "ERROR"


class TestOneshotMode:
    @pytest.fixture
    def plugin(self, make_plugin):
        return make_plugin(SystemdCollectorPlugin, ONESHOT_CFG)

    async def test_successful_recent_run_is_online(self, plugin):
        _run(plugin, [(0, _oneshot_output("success", "0"), ""), (0, "logs", "")])
        assert _latest_status("test-upgrade") == "online"

    async def test_never_run_is_failed(self, plugin):
        _run(plugin, [(0, "result=empty exit=empty epoch=0 active=inactive sub=dead", ""), (0, "", "")])
        assert _latest_status("test-upgrade") == "failed"

    async def test_failed_result_is_failed(self, plugin):
        _run(plugin, [(0, _oneshot_output("failed", "1"), ""), (0, "", "")])
        assert _latest_status("test-upgrade") == "failed"

    async def test_stale_run_exceeding_max_age_is_failed(self, plugin):
        old_epoch = int(time.time()) - 14 * 24 * 3600
        _run(plugin, [(0, _oneshot_output("success", "0", epoch=old_epoch), ""), (0, "", "")])
        assert _latest_status("test-upgrade") == "failed"

    async def test_run_within_max_age_is_online(self, plugin):
        recent_epoch = int(time.time()) - 3 * 24 * 3600
        _run(plugin, [(0, _oneshot_output("success", "0", epoch=recent_epoch), ""), (0, "", "")])
        assert _latest_status("test-upgrade") == "online"

    async def test_currently_running_is_online(self, plugin):
        _run(plugin, [(0, _oneshot_output(active="activating", sub="start"), ""), (0, "", "")])
        assert _latest_status("test-upgrade") == "online"
        assert _latest_metric("test-upgrade", "is_running") == pytest.approx(1.0)

    async def test_active_running_substate_is_running(self, plugin):
        _run(plugin, [(0, _oneshot_output(active="active", sub="running"), ""), (0, "", "")])
        assert _latest_metric("test-upgrade", "is_running") == pytest.approx(1.0)

    async def test_inactive_not_marked_running(self, plugin):
        _run(plugin, [(0, _oneshot_output("success", "0", active="inactive", sub="dead"), ""), (0, "", "")])
        assert _latest_metric("test-upgrade", "is_running") == pytest.approx(0.0)

    async def test_success_via_exit_code_zero_overrides_result(self, plugin):
        _run(plugin, [(0, _oneshot_output("exit-code", "0"), ""), (0, "", "")])
        assert _latest_status("test-upgrade") == "online"

    async def test_ssh_command_failure_sets_failed(self, plugin):
        _run(plugin, [(-1, "", "SSH timeout"), (-1, "", "SSH timeout")])
        assert _latest_status("test-upgrade") == "failed"

    async def test_last_run_epoch_metric_recorded(self, plugin):
        epoch = int(time.time()) - 100
        _run(plugin, [(0, _oneshot_output("success", "0", epoch=epoch), ""), (0, "", "")])
        assert _latest_metric("test-upgrade", "last_run_epoch") == pytest.approx(float(epoch))

    async def test_last_run_success_metric_recorded_on_success(self, plugin):
        _run(plugin, [(0, _oneshot_output("success", "0"), ""), (0, "", "")])
        assert _latest_metric("test-upgrade", "last_run_success") == pytest.approx(1.0)

    async def test_last_run_success_metric_recorded_on_failure(self, plugin):
        _run(plugin, [(0, _oneshot_output("failed", "1"), ""), (0, "", "")])
        assert _latest_metric("test-upgrade", "last_run_success") == pytest.approx(0.0)


class TestMaxAgeParsing:
    def test_max_age_parsed_from_human_string(self, make_plugin):
        plugin = make_plugin(SystemdCollectorPlugin, {**ONESHOT_CFG, "max_age": "1w"})
        assert plugin.max_age == 604800

    def test_max_age_parsed_from_int(self, make_plugin):
        plugin = make_plugin(SystemdCollectorPlugin, {**ONESHOT_CFG, "max_age": 3600})
        assert plugin.max_age == 3600

    def test_no_max_age_means_continuous_mode(self, make_plugin):
        plugin = make_plugin(SystemdCollectorPlugin, CONTINUOUS_CFG)
        assert plugin.max_age is None


class TestActions:
    @pytest.fixture
    def plugin(self, make_plugin):
        return make_plugin(SystemdCollectorPlugin, CONTINUOUS_CFG)

    async def test_restart_success(self, plugin):
        plan = plugin.plan_action("restart_service")
        assert plan.command == "sudo systemctl restart nginx.service"
        assert plugin.interpret_action("restart_service", CmdResult(0, "", "")) is True

    async def test_restart_failure(self, plugin):
        result = plugin.interpret_action("restart_service", CmdResult(1, "", "Unit not found"))
        assert result.success is False

    async def test_stop_success(self, plugin):
        plan = plugin.plan_action("stop_service")
        assert plan.command == "sudo systemctl stop nginx.service"
        assert plugin.interpret_action("stop_service", CmdResult(0, "", "")) is True

    async def test_disable_success(self, plugin):
        plan = plugin.plan_action("disable_service")
        assert plan.command == "sudo systemctl disable nginx.service"
        assert plugin.interpret_action("disable_service", CmdResult(0, "", "")) is True

    async def test_disable_failure(self, plugin):
        result = plugin.interpret_action("disable_service", CmdResult(1, "", "error"))
        assert result.success is False

    async def test_unknown_action_returns_false(self, plugin):
        assert plugin.plan_action("nuke") is None

    def test_get_actions_includes_restart_stop_enable_disable(self, plugin):
        ids = {a["action_id"] for a in plugin.get_actions()}
        assert "restart_service" in ids
        assert "stop_service" in ids
        assert "enable_service" in ids
        assert "disable_service" in ids
