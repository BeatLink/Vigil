import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.containers import ContainersCollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


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
    async def test_all_running_online(self, make_plugin, run_cycle):
        p = make_plugin(ContainersCollectorPlugin, _cfg())
        run_cycle(p, lambda c: CmdResult(0, _ps(("web", "running"), ("db", "running")), ""))
        assert _latest_status() == "online"
        assert _latest_metric("containers_running") == pytest.approx(2.0)
        assert _latest_metric("containers_stopped") == pytest.approx(0.0)

    async def test_stopped_container_warning(self, make_plugin, run_cycle):
        p = make_plugin(ContainersCollectorPlugin, _cfg())
        run_cycle(p, lambda c: CmdResult(0, _ps(("web", "running"), ("db", "exited")), ""))
        assert _latest_status() == "warning"
        assert _latest_metric("containers_stopped") == pytest.approx(1.0)

    async def test_stopped_warning_disabled_online(self, make_plugin, run_cycle):
        p = make_plugin(ContainersCollectorPlugin, _cfg(stopped_warning=False))
        run_cycle(p, lambda c: CmdResult(0, _ps(("web", "running"), ("db", "exited")), ""))
        assert _latest_status() == "online"

    async def test_expected_missing_failed(self, make_plugin, run_cycle):
        p = make_plugin(ContainersCollectorPlugin, _cfg(expect_running=["db"]))
        run_cycle(p, lambda c: CmdResult(0, _ps(("web", "running"), ("db", "exited")), ""))
        assert _latest_status() == "failed"

    async def test_expected_running_online(self, make_plugin, run_cycle):
        p = make_plugin(ContainersCollectorPlugin, _cfg(expect_running=["web", "db"]))
        run_cycle(p, lambda c: CmdResult(0, _ps(("web", "running"), ("db", "running")), ""))
        assert _latest_status() == "online"

    async def test_paused_is_benign(self, make_plugin, run_cycle):
        p = make_plugin(ContainersCollectorPlugin, _cfg())
        run_cycle(p, lambda c: CmdResult(0, _ps(("web", "running"), ("job", "paused")), ""))
        assert _latest_status() == "online"

    async def test_no_containers_online(self, make_plugin, run_cycle):
        p = make_plugin(ContainersCollectorPlugin, _cfg())
        run_cycle(p, lambda c: CmdResult(0, "", ""))
        assert _latest_status() == "online"

    async def test_runtime_missing_offline(self, make_plugin, run_cycle):
        p = make_plugin(ContainersCollectorPlugin, _cfg())
        run_cycle(p, lambda c: CmdResult(127, "", "bash: docker: command not found"))
        assert _latest_status() == "offline"

    async def test_podman_runtime_used(self, make_plugin, run_cycle):
        p = make_plugin(ContainersCollectorPlugin, _cfg(runtime="podman"))
        commands = p.commands()
        assert commands[0].text.startswith("podman ")
        run_cycle(p, lambda c: CmdResult(0, _ps(("web", "running")), ""))
        assert _latest_status() == "online"


class TestContainersActions:
    async def test_restart_listed_container(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg(expect_running=["web"]))
        plan = p.plan_action("restart:web")
        assert "docker restart" in plan.command and "web" in plan.command
        assert p.interpret_action("restart:web", CmdResult(0, "", "")) is True

    async def test_refuse_unlisted_container(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg(expect_running=["web"]))
        plan = p.plan_action("restart:evil")
        assert plan.success is False
        assert plan.logs

    async def test_actions_list_from_expected(self, make_plugin):
        p = make_plugin(ContainersCollectorPlugin, _cfg(expect_running=["web", "db"]))
        ids = {a["action_id"] for a in p.get_actions()}
        assert ids == {"restart:web", "restart:db"}
