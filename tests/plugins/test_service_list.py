import pytest
from unittest.mock import AsyncMock
from vigil.plugins.service_list import ServiceListCollectorPlugin
from vigil.core.data.database import db, StatusHistory, Metric, flush_writes

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
        return make_plugin(ServiceListCollectorPlugin, CFG)

    async def test_collects_services_and_metrics(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(side_effect=[
            (0, SERVICE_LIST_OUTPUT, ""),
            (0, UNIT_FILE_OUTPUT, ""),
        ])
        await plugin.on_collect()
        assert _latest_metric("services_total") == pytest.approx(3.0)
        assert _latest_metric("services_active") == pytest.approx(2.0)
        assert _latest_metric("services_failed") == pytest.approx(0.0)

    async def test_start_service_action(self, plugin):
        plugin.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        assert await plugin.on_action("start_service", service_name="nginx.service") is True
        plugin.ssh_controller.execute_action.assert_called_once_with(
            "sudo systemctl start nginx.service"
        )

    async def test_view_status_action_fails_without_service(self, plugin):
        assert await plugin.on_action("view_status") is False

    async def test_unknown_action_returns_false(self, plugin):
        assert await plugin.on_action("nuke_service", service_name="nginx.service") is False

    def test_get_actions_returns_reload(self, plugin):
        actions = {a['action_id'] for a in plugin.get_actions()}
        assert 'daemon_reload' in actions
