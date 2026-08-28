import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.memory import Memory
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-memory", "id": "test-memory", "ssh_config": {"host": "test.host"}}

_MEM_TOTAL_KB = 16_000_000


def _meminfo(avail_kb):
    return f"MemTotal:       {_MEM_TOTAL_KB} kB\nMemAvailable:   {avail_kb} kB\n"



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-memory"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-memory") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Memory, CFG)


class TestCollection:
    async def test_usage_is_the_share_that_is_unavailable(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _meminfo(8_000_000), ""))
        assert _latest_status() == "online"
        assert _latest_metric("memory_pct") == pytest.approx(50.0)

    async def test_thresholds_are_configurable(self, make_plugin, run_cycle):
        p = make_plugin(Memory, dict(CFG, warning=40, threshold=90))
        run_cycle(p, lambda c: CmdResult(0, _meminfo(8_000_000), ""))
        assert _latest_status() == "warning"

    async def test_exhausted_memory_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _meminfo(400_000), ""))
        assert _latest_status() == "failed"

    async def test_a_failed_command_fails_the_monitor(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "boom"))
        assert _latest_status() == "failed"
