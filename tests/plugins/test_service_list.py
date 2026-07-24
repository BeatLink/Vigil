import pytest
from unittest.mock import AsyncMock
from vigil.plugins.service_list import ServiceList
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric, flush_writes

CFG = {
    "name": "service-browser",
    "id": "service-browser",
    "interval": 60,
    "ssh_config": {"host": "test.host"},
}

SERVICE_LIST_OUTPUT = """
nginx.service loaded active running Nginx HTTP Server
ssh.service loaded active running OpenSSH Daemon
avahi-daemon.service loaded inactive dead Avahi mDNS/DNS-SD Stack
"""

UNIT_FILE_OUTPUT = """
nginx.service enabled
ssh.service enabled
avahi-daemon.service disabled
"""


def _latest_metric(metric: str) -> float | None:
    flush_writes()
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "service-browser") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestServiceListPlugin:
    @pytest.fixture
    def plugin(self, make_plugin):
        return make_plugin(ServiceList, CFG)

    async def test_collects_services_and_metrics(self, plugin, run_cycle):
        outputs = [
            CmdResult(0, SERVICE_LIST_OUTPUT, ""),
            CmdResult(0, UNIT_FILE_OUTPUT, ""),
        ]
        run_cycle(plugin, lambda c, _it=iter(outputs): next(_it))
        assert _latest_metric("services_total") == pytest.approx(3.0)
        assert _latest_metric("services_active") == pytest.approx(2.0)
        assert _latest_metric("services_failed") == pytest.approx(0.0)

    async def test_start_service_action(self, plugin):
        plan = plugin.plan_action("start_service", service_name="nginx.service")
        assert plan.command == "sudo systemctl start nginx.service"
        assert plugin.interpret_action("start_service", CmdResult(0, "", "")) is True

    async def test_view_status_action_fails_without_service(self, plugin):
        plan = plugin.plan_action("view_status")
        assert plan.success is False

    async def test_view_status_action_plans_status_command(self, plugin):
        plan = plugin.plan_action("view_status", service_name="nginx.service")
        assert plan.command == "sudo systemctl status nginx.service --no-pager"

    async def test_view_status_interprets_output_as_content(self, plugin):
        outcome = plugin.interpret_action("view_status", CmdResult(0, "Active: active", ""))
        assert outcome.success is True
        assert outcome.metadata == {'content': 'Active: active'}

    async def test_view_unit_file_action_plans_cat_command(self, plugin):
        plan = plugin.plan_action("view_unit_file", service_name="nginx.service")
        assert plan.command == "sudo systemctl cat nginx.service"

    async def test_write_unit_file_plans_path_resolve_and_base64_write(self, plugin):
        import base64
        plan = plugin.plan_action("write_unit_file", service_name="nginx.service", content="[Unit]\n")
        assert "systemctl show -p FragmentPath" in plan.command
        assert base64.b64encode(b"[Unit]\n").decode('ascii') in plan.command

    async def test_unknown_action_returns_false(self, plugin):
        assert plugin.plan_action("nuke_service", service_name="nginx.service") is None

    def test_get_actions_returns_reload(self, plugin):
        actions = {a['action_id'] for a in plugin.get_actions()}
        assert 'daemon_reload' in actions
