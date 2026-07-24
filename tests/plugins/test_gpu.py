import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.gpu import Gpu
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-gpu",
    "id":   "test-gpu",
    "util_warning": 85, "util_threshold": 95,
    "mem_warning": 85,  "mem_threshold": 95,
    "temp_warning": 80, "temp_threshold": 90,
    "ssh_config": {"host": "test.host"},
}


def _row(idx, util, mem_used, mem_total, temp):
    return f"{idx}, {util}, {mem_used}, {mem_total}, {temp}"


def _make_output(rows):
    return "\n".join(_row(*r) for r in rows) + "\n"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Gpu, BASE_CFG)


def _latest_status(pid="test-gpu"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric, name="test-gpu"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestGpuCollection:
    async def test_idle_gpu_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output([(0, 10, 1000, 8000, 45)]), ""))
        assert _latest_status() == "online"
        assert _latest_metric("gpu_util") == pytest.approx(10.0)
        assert _latest_metric("gpu_mem_pct") == pytest.approx(12.5)
        assert _latest_metric("gpu_temp") == pytest.approx(45.0)

    async def test_high_util_warning(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output([(0, 88, 1000, 8000, 50)]), ""))
        assert _latest_status() == "warning"

    async def test_hot_gpu_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output([(0, 10, 1000, 8000, 92)]), ""))
        assert _latest_status() == "failed"

    async def test_full_vram_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output([(0, 10, 7800, 8000, 50)]), ""))
        assert _latest_status() == "failed"

    async def test_worst_of_multiple_gpus(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output([(0, 10, 1000, 8000, 50),
                                                                (1, 99, 1000, 8000, 50)]), ""))
        assert _latest_status() == "failed"
        assert _latest_metric("gpu_util") == pytest.approx(99.0)
        assert _latest_metric("gpu1_util") == pytest.approx(99.0)

    async def test_nvidia_smi_missing_offline(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(127, "", "bash: nvidia-smi: command not found"))
        assert _latest_status() == "offline"

    async def test_no_devices_offline(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(9, "", "No devices were found"))
        assert _latest_status() == "offline"

    async def test_generic_failure_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "some driver error"))
        assert _latest_status() == "failed"


class TestGpuActions:
    async def test_on_action_returns_none(self, plugin):
        assert plugin.plan_action("anything") is None
