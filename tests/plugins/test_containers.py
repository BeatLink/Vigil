import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.containers import ContainersCollectorPlugin
from vigil.core.data.database import db, StatusHistory, Metric


def _latest_status(pid="test-ctr"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric, name="test-ctr"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _cfg(**extra):
    base = {"name": "test-ctr", "id": "test-ctr", "ssh_config": {"host": "test.host"}}
    base.update(extra)
    return base


def _ps(*rows):
    return "".join(f"{name}\t{state}\n" for name, state in rows)


class TestContainersCollection:
    async def test_all_running_online(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg())
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _ps(("web", "running"), ("db", "running")), ""))
        await p.on_collect()
        assert _latest_status() == "online"
        assert _latest_metric("containers_running") == pytest.approx(2.0)
        assert _latest_metric("containers_stopped") == pytest.approx(0.0)

    async def test_stopped_container_warning(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg())
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _ps(("web", "running"), ("db", "exited")), ""))
        await p.on_collect()
        assert _latest_status() == "warning"
        assert _latest_metric("containers_stopped") == pytest.approx(1.0)

    async def test_stopped_warning_disabled_online(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg(stopped_warning=False))
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _ps(("web", "running"), ("db", "exited")), ""))
        await p.on_collect()
        assert _latest_status() == "online"

    async def test_expected_missing_failed(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg(expect_running=["db"]))
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _ps(("web", "running"), ("db", "exited")), ""))
        await p.on_collect()
        assert _latest_status() == "failed"

    async def test_expected_running_online(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg(expect_running=["web", "db"]))
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _ps(("web", "running"), ("db", "running")), ""))
        await p.on_collect()
        assert _latest_status() == "online"

    async def test_paused_is_benign(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg())
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _ps(("web", "running"), ("job", "paused")), ""))
        await p.on_collect()
        assert _latest_status() == "online"

    async def test_no_containers_online(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg())
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, "", ""))
        await p.on_collect()
        assert _latest_status() == "online"

    async def test_runtime_missing_offline(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg())
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(127, "", "bash: docker: command not found"))
        await p.on_collect()
        assert _latest_status() == "offline"

    async def test_podman_runtime_used(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg(runtime="podman"))
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _ps(("web", "running")), ""))
        await p.on_collect()
        # Command should invoke podman
        called = p.ssh_collector.fetch_output.call_args[0][0]
        assert called.startswith("podman ")
        assert _latest_status() == "online"


class TestContainersActions:
    async def test_restart_listed_container(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg(expect_running=["web"]))
        p.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        assert await p.on_action("restart:web") is True
        cmd = p.ssh_controller.execute_action.call_args[0][0]
        assert "docker restart" in cmd and "web" in cmd

    async def test_refuse_unlisted_container(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg(expect_running=["web"]))
        p.ssh_controller.execute_action = AsyncMock(return_value=(0, "", ""))
        assert await p.on_action("restart:evil") is False
        p.ssh_controller.execute_action.assert_not_called()

    async def test_actions_list_from_expected(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg(expect_running=["web", "db"]))
        ids = {a["action_id"] for a in p.get_actions()}
        assert ids == {"restart:web", "restart:db"}
