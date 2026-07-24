import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.interrupts import InterruptsCollectorPlugin, _extract_counter
from vigil.collector.orchestration.types import CmdResult
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-irq",
    "id":   "test-irq",
    "ssh_config": {"host": "test.host"},
}


def _make_stat(intr: int, ctxt: int) -> str:
    return (
        "cpu  100 0 50 900 10 0 5 0 0 0\n"
        f"intr {intr} 20 0 0 0 0\n"
        "ctxt {ctxt}\n".replace("{ctxt}", str(ctxt))
    )


def _two_snaps(s1: str, s2: str) -> str:
    return s1 + "---SNAP---\n" + s2


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(InterruptsCollectorPlugin, BASE_CFG)


def _latest_status(plugin_id: str = "test-irq"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(name: str, metric: str):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestExtractCounter:
    def test_reads_intr_total(self):
        assert _extract_counter(_make_stat(12345, 999), "intr") == 12345

    def test_reads_ctxt(self):
        assert _extract_counter(_make_stat(1, 54321), "ctxt") == 54321

    def test_missing_key_returns_none(self):
        assert _extract_counter("cpu 1 2 3\n", "intr") is None


class TestInterruptsCollection:
    async def test_normal_online(self, plugin, run_cycle):
        stdout = _two_snaps(_make_stat(1000, 500), _make_stat(6000, 2500))
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "online"
        assert _latest_metric("test-irq", "irq_per_sec") == pytest.approx(5000.0)
        assert _latest_metric("test-irq", "ctxt_per_sec") == pytest.approx(2000.0)

    async def test_warning_threshold(self, make_plugin, run_cycle):
        p = make_plugin(InterruptsCollectorPlugin, {**BASE_CFG, "irq_warning": 100, "irq_threshold": 10000})
        stdout = _two_snaps(_make_stat(0, 0), _make_stat(500, 0))
        run_cycle(p, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "warning"

    async def test_failed_threshold(self, make_plugin, run_cycle):
        p = make_plugin(InterruptsCollectorPlugin, {**BASE_CFG, "irq_warning": 100, "irq_threshold": 1000})
        stdout = _two_snaps(_make_stat(0, 0), _make_stat(5000, 0))
        run_cycle(p, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "failed"

    async def test_counter_reset_clamped(self, plugin, run_cycle):
        stdout = _two_snaps(_make_stat(9000, 9000), _make_stat(10, 10))
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_metric("test-irq", "irq_per_sec") == pytest.approx(0.0)

    async def test_malformed_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "no separator", ""))
        assert _latest_status() == "failed"

    async def test_missing_intr_fails(self, plugin, run_cycle):
        stdout = _two_snaps("cpu 1 2 3\n", "cpu 1 2 3\n")
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "failed"

    async def test_ssh_failure_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(-1, "", "err"))
        assert _latest_status() == "failed"


class TestInterruptsActions:
    async def test_on_action_returns_none(self, plugin):
        assert plugin.plan_action("anything") is None
