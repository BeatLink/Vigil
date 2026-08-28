import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.load import Load
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-load", "id": "test-load", "ssh_config": {"host": "test.host"}}

def _loadavg(one, five=0.5, fifteen=0.5, cpus=4):
    return f"LOAD:{one} {five} {fifteen} 1/500 12345\nCPUS:{cpus}\n"



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-load"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-load") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Load, CFG)


class TestCollection:
    async def test_load_is_scaled_by_core_count(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _loadavg(1.0, cpus=4), ""))
        assert _latest_status() == "online"
        assert _latest_metric("load_pct_1m") == pytest.approx(25.0)

    async def test_without_thresholds_any_load_stays_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _loadavg(16.0, cpus=4), ""))
        assert _latest_status() == "online"
        assert _latest_metric("load_pct_1m") == pytest.approx(400.0)

    async def test_thresholds_are_configurable(self, make_plugin, run_cycle):
        p = make_plugin(Load, dict(CFG, warning=100, threshold=200))
        run_cycle(p, lambda c: CmdResult(0, _loadavg(12.0, cpus=4), ""))
        assert _latest_status() == "failed"

    async def test_a_failed_command_fails_the_monitor(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "boom"))
        assert _latest_status() == "failed"
