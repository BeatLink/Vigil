import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.command import CommandCollectorPlugin
from vigil.collector.orchestration.types import CmdResult
from vigil.core.data.database import db, StatusHistory, Metric


def _latest_status(pid):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric, name):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _cfg(**extra):
    base = {"name": "test-cmd", "id": "test-cmd", "ssh_config": {"host": "test.host"}}
    base.update(extra)
    return base


class TestExitCodeMode:
    async def test_zero_exit_online(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(command="true"))
        run_cycle(p, lambda c: CmdResult(0, "ok", ""))
        assert _latest_status("test-cmd") == "online"
        assert _latest_metric("exit_code", "test-cmd") == pytest.approx(0.0)

    async def test_nonzero_exit_failed(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(command="false"))
        run_cycle(p, lambda c: CmdResult(3, "", "boom"))
        assert _latest_status("test-cmd") == "failed"

    async def test_nonzero_is_warning_flag(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(command="false", nonzero_is_warning=True))
        run_cycle(p, lambda c: CmdResult(1, "", ""))
        assert _latest_status("test-cmd") == "warning"

    async def test_timeout_failed(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(command="sleep 100", timeout=1))
        run_cycle(p, lambda c: CmdResult(124, "", ""))
        assert _latest_status("test-cmd") == "failed"

    async def test_missing_command_failed(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg())
        run_cycle(p, lambda c: CmdResult(0, "", ""))
        assert _latest_status("test-cmd") == "failed"


class TestPatternMode:
    async def test_value_below_warning_online(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(
            command="get_temp", pattern=r"temp=(\d+)", warning=70, threshold=80))
        run_cycle(p, lambda c: CmdResult(0, "temp=42", ""))
        assert _latest_status("test-cmd") == "online"
        assert _latest_metric("value", "test-cmd") == pytest.approx(42.0)

    async def test_value_between_warning(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(
            command="x", pattern=r"temp=(\d+)", warning=70, threshold=80))
        run_cycle(p, lambda c: CmdResult(0, "temp=75", ""))
        assert _latest_status("test-cmd") == "warning"

    async def test_value_above_threshold_failed(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(
            command="x", pattern=r"temp=(\d+)", warning=70, threshold=80))
        run_cycle(p, lambda c: CmdResult(0, "temp=95", ""))
        assert _latest_status("test-cmd") == "failed"

    async def test_no_match_failed(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(
            command="x", pattern=r"temp=(\d+)", warning=70, threshold=80))
        run_cycle(p, lambda c: CmdResult(0, "nothing here", ""))
        assert _latest_status("test-cmd") == "failed"

    async def test_invert_low_value_is_bad(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(
            command="x", pattern=r"free=(\d+)", warning=20, threshold=10, invert=True))
        run_cycle(p, lambda c: CmdResult(0, "free=5", ""))
        assert _latest_status("test-cmd") == "failed"

    async def test_invert_high_value_online(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(
            command="x", pattern=r"free=(\d+)", warning=20, threshold=10, invert=True))
        run_cycle(p, lambda c: CmdResult(0, "free=90", ""))
        assert _latest_status("test-cmd") == "online"

    async def test_float_value_parsed(self, make_plugin, run_cycle):
        p = make_plugin(CommandCollectorPlugin, _cfg(
            command="x", pattern=r"load=([\d.]+)", warning=4, threshold=8))
        run_cycle(p, lambda c: CmdResult(0, "load=2.5", ""))
        assert _latest_metric("value", "test-cmd") == pytest.approx(2.5)


class TestActions:
    async def test_on_action_returns_false(self, make_plugin):
        p = make_plugin(CommandCollectorPlugin, _cfg(command="true"))
        assert p.plan_action("anything") is None
