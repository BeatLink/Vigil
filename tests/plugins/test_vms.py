import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.vms import VmsCollectorPlugin, _parse_row
from vigil.core.data.database import db, StatusHistory, Metric


def _latest_status(pid="test-vms"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric, name="test-vms"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _cfg(**extra):
    base = {"name": "test-vms", "id": "test-vms", "ssh_config": {"host": "test.host"}}
    base.update(extra)
    return base


_LIST = """ Id   Name       State
----------------------------------
 1    web        running
 2    cache      running
 -    db         shut off
"""

_LIST_PAUSED = """ Id   Name       State
----------------------------------
 1    web        running
 3    stuck      paused
"""


class TestParseRow:
    def test_header_ignored(self):
        assert _parse_row(" Id   Name       State") == (None, None)

    def test_separator_ignored(self):
        assert _parse_row("---------------------------") == (None, None)

    def test_running_row(self):
        assert _parse_row(" 1    web        running") == ("web", "running")

    def test_two_word_state(self):
        assert _parse_row(" -    db         shut off") == ("db", "shut off")


class TestVmsCollection:
    async def test_running_and_off_online(self, make_plugin):
        p = make_plugin(VmsCollectorPlugin, _cfg())
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, _LIST, ""))
        await p.on_collect()
        assert _latest_status() == "online"
        assert _latest_metric("vms_running") == pytest.approx(2.0)
        assert _latest_metric("vms_total") == pytest.approx(3.0)

    async def test_paused_is_warning(self, make_plugin):
        p = make_plugin(VmsCollectorPlugin, _cfg())
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, _LIST_PAUSED, ""))
        await p.on_collect()
        assert _latest_status() == "warning"

    async def test_expected_off_failed(self, make_plugin):
        p = make_plugin(VmsCollectorPlugin, _cfg(expect_running=["db"]))
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, _LIST, ""))
        await p.on_collect()
        assert _latest_status() == "failed"

    async def test_expected_running_online(self, make_plugin):
        p = make_plugin(VmsCollectorPlugin, _cfg(expect_running=["web", "cache"]))
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, _LIST, ""))
        await p.on_collect()
        assert _latest_status() == "online"

    async def test_virsh_missing_offline(self, make_plugin):
        p = make_plugin(VmsCollectorPlugin, _cfg())
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(127, "", "bash: virsh: command not found"))
        await p.on_collect()
        assert _latest_status() == "offline"

    async def test_libvirt_unreachable_failed(self, make_plugin):
        p = make_plugin(VmsCollectorPlugin, _cfg())
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(1, "", "error: failed to connect to the hypervisor"))
        await p.on_collect()
        assert _latest_status() == "failed"


class TestVmsActions:
    async def test_start_listed_vm(self, make_plugin):
        p = make_plugin(VmsCollectorPlugin, _cfg(expect_running=["web"]))
        p.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        assert await p.on_action("start:web") is True
        cmd = p.ssh_controller.execute_action.call_args[0][0]
        assert "start" in cmd and "web" in cmd

    async def test_refuse_unlisted_vm(self, make_plugin):
        p = make_plugin(VmsCollectorPlugin, _cfg(expect_running=["web"]))
        p.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        assert await p.on_action("start:evil") is False
        p.ssh_controller.execute_action.assert_not_called()

    async def test_actions_list(self, make_plugin):
        p = make_plugin(VmsCollectorPlugin, _cfg(expect_running=["web"]))
        ids = {a["action_id"] for a in p.get_actions()}
        assert ids == {"start:web", "shutdown:web"}
