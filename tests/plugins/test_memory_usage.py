import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.memory_usage import MemoryUsage, _level_for, _fmt_gb
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-memory",
    "id":   "test-memory",
    "memory_warning":   75,
    "memory_threshold": 90,
    "ssh_config": {"host": "test.host"},
}

_MEM_TOTAL_KB = 16_000_000
_MEM_AVAIL_50 = 8_000_000
_MEM_AVAIL_20 = 3_200_000
_MEM_AVAIL_5  = 800_000


def _make_output(total_kb, avail_kb):
    return f"MemTotal:       {total_kb} kB\nMemAvailable:   {avail_kb} kB\n"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(MemoryUsage, BASE_CFG)


def _latest_status(plugin_id: str = "test-memory") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-memory") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestFmtGb:
    def test_less_than_1024_shows_gb(self):
        assert _fmt_gb(512.0) == "512.0 GB"

    def test_1024_shows_tb(self):
        assert _fmt_gb(1024.0) == "1.0 TB"


class TestLevelFor:
    def test_below_warning_is_online(self):
        assert _level_for(50.0, 75.0, 90.0) == 'online'

    def test_at_warning_is_warning(self):
        assert _level_for(75.0, 75.0, 90.0) == 'warning'

    def test_between_thresholds_is_warning(self):
        assert _level_for(80.0, 75.0, 90.0) == 'warning'

    def test_at_failed_is_failed(self):
        assert _level_for(90.0, 75.0, 90.0) == 'failed'


class TestMemoryUsageCollection:
    async def test_below_warning_sets_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output(_MEM_TOTAL_KB, _MEM_AVAIL_50), ""))
        assert _latest_status() == "online"

    async def test_above_warning_sets_warning(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output(_MEM_TOTAL_KB, _MEM_AVAIL_20), ""))
        assert _latest_status() == "warning"

    async def test_above_threshold_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output(_MEM_TOTAL_KB, _MEM_AVAIL_5), ""))
        assert _latest_status() == "failed"

    async def test_memory_pct_metric_recorded(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output(_MEM_TOTAL_KB, _MEM_AVAIL_50), ""))
        assert _latest_metric("memory_pct") == pytest.approx(50.0)

    async def test_memory_used_gb_metric_recorded(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output(_MEM_TOTAL_KB, _MEM_AVAIL_50), ""))
        expected = (_MEM_TOTAL_KB - _MEM_AVAIL_50) / (1024 ** 2)
        assert _latest_metric("memory_used_gb") == pytest.approx(expected, rel=1e-3)

    async def test_memory_total_gb_metric_recorded(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_output(_MEM_TOTAL_KB, _MEM_AVAIL_50), ""))
        expected = _MEM_TOTAL_KB / (1024 ** 2)
        assert _latest_metric("memory_total_gb") == pytest.approx(expected, rel=1e-3)

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(-1, "", "timeout"))
        assert _latest_status() == "failed"

    async def test_missing_memavailable_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "MemTotal: 16000000 kB\n", ""))
        assert _latest_status() == "failed"

    async def test_missing_memtotal_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "MemAvailable: 8000000 kB\n", ""))
        assert _latest_status() == "failed"

    async def test_custom_thresholds_respected(self, make_plugin, run_cycle):
        cfg = {**BASE_CFG, "name": "test-mem-custom", "id": "test-mem-custom",
               "memory_warning": 40, "memory_threshold": 50}
        p = make_plugin(MemoryUsage, cfg)
        run_cycle(p, lambda c: CmdResult(0, _make_output(_MEM_TOTAL_KB, _MEM_AVAIL_50), ""))
        assert _latest_status("test-mem-custom") == "failed"


class TestMemoryUsageActions:
    async def test_on_action_always_returns_none(self, plugin):
        assert plugin.plan_action("anything") is None
