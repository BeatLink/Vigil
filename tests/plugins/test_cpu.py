import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.cpu import Cpu
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-cpu", "id": "test-cpu", "ssh_config": {"host": "test.host"}}


def _proc_stat(idle_delta=500, total_delta=1000):
    """Two /proc/stat samples, the second `total_delta` jiffies later with
    `idle_delta` of them idle."""
    return (f"cpu  1000 0 0 1000 0 0 0 0\n"
            f"cpu  {1000 + total_delta - idle_delta} 0 0 {1000 + idle_delta} 0 0 0 0\n")


def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-cpu"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-cpu") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Cpu, CFG)


class TestCollection:
    async def test_busy_cpu_crosses_thresholds(self, make_plugin, run_cycle):
        p = make_plugin(Cpu, dict(CFG, warning=70, threshold=85))
        run_cycle(p, lambda c: CmdResult(0, _proc_stat(idle_delta=100), ""))
        assert _latest_status() == "failed"
        assert _latest_metric("cpu_pct") == pytest.approx(90.0)

    async def test_idle_cpu_is_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _proc_stat(idle_delta=1000), ""))
        assert _latest_status() == "online"
        assert _latest_metric("cpu_pct") == pytest.approx(0.0)

    async def test_a_failed_command_fails_the_monitor(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "boom"))
        assert _latest_status() == "failed"

    async def test_incomplete_output_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "cpu  1 2 3 4 0 0 0 0\n", ""))
        assert _latest_status() == "failed"

    def test_one_command_per_cycle(self, plugin):
        assert len(plugin.commands()) == 1
