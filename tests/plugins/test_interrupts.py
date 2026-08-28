import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.interrupts import Interrupts
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-interrupts", "id": "test-interrupts", "ssh_config": {"host": "test.host"}}

def _snaps(intr_delta=1000, ctxt_delta=2000):
    def snap(intr, ctxt):
        return f"cpu  100 0 50 900 10 0 5 0\nintr {intr} 20 0 0\nctxt {ctxt}\n"
    return snap(1_000_000, 5_000_000) + "---SNAP---\n" + snap(
        1_000_000 + intr_delta, 5_000_000 + ctxt_delta)



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-interrupts"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-interrupts") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Interrupts, CFG)


class TestCollection:
    async def test_rates_are_the_delta_between_snapshots(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _snaps(intr_delta=1500), ""))
        assert _latest_status() == "online"
        assert _latest_metric("irq_per_sec") == pytest.approx(1500.0)
        assert _latest_metric("ctxt_per_sec") == pytest.approx(2000.0)

    async def test_high_rate_crosses_thresholds(self, make_plugin, run_cycle):
        p = make_plugin(Interrupts, dict(CFG, warning=500, threshold=1000))
        run_cycle(p, lambda c: CmdResult(0, _snaps(intr_delta=1200), ""))
        assert _latest_status() == "failed"

    async def test_unexpected_output_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "cpu 1 2 3\n", ""))
        assert _latest_status() == "failed"

    def test_it_takes_its_own_proc_stat_sample(self, plugin):
        assert '---SNAP---' in plugin.commands()[0].text
