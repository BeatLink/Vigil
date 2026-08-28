import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.gpu import Gpu
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-gpu", "id": "test-gpu", "ssh_config": {"host": "test.host"}}

def _nvidia_smi(util=10.0, mem_used=1000, mem_total=8000, temp=50.0, count=1):
    return "".join(
        f"{idx}, {util}, {mem_used}, {mem_total}, {temp}\n" for idx in range(count))



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-gpu"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-gpu") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Gpu, CFG)


class TestCollection:
    async def test_peak_across_cards_is_reported(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _nvidia_smi(util=40.0, temp=60.0, count=2), ""))
        assert _latest_status() == "online"
        assert _latest_metric("gpu_util") == pytest.approx(40.0)
        assert _latest_metric("gpu1_util") == pytest.approx(40.0)
        assert _latest_metric("gpu_temp") == pytest.approx(60.0)

    async def test_hot_gpu_fails(self, make_plugin, run_cycle):
        p = make_plugin(Gpu, dict(CFG, temp_threshold=55))
        run_cycle(p, lambda c: CmdResult(0, _nvidia_smi(temp=60.0), ""))
        assert _latest_status() == "failed"

    async def test_missing_nvidia_smi_is_offline(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(127, "", "nvidia-smi: command not found"))
        assert _latest_status() == "offline"


class TestTimeoutBreaker:
    async def test_repeated_timeouts_suspend_the_probe(self, make_plugin, run_cycle):
        p = make_plugin(Gpu, dict(CFG, timeout_trip=2, suspend_seconds=600))
        timed_out = lambda c: CmdResult(1, "", "Command timed out after 15s")
        run_cycle(p, timed_out)
        assert _latest_status() == "failed"
        run_cycle(p, timed_out)
        assert _latest_status() == "offline"
        assert p.commands() == []           # breaker open: nothing issued at all
        run_cycle(p, timed_out)
        assert _latest_status() == "offline"


class TestUiSpec:
    def test_per_card_repeat_gets_its_own_row(self, plugin):
        spec = plugin.UI_SPEC
        assert 'gpus' not in spec['layout'][0]
        assert ['gpus'] in spec['layout']
