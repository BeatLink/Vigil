import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.cpu_usage import CpuUsageCollectorPlugin, _parse_cpu_line, _cpu_pct, _level_for
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-cpu",
    "id":   "test-cpu",
    "cpu_warning":   70,
    "cpu_threshold": 85,
    "ssh_config": {"host": "test.host"},
}

_CPU1_50 = [100, 0, 0, 900, 0, 0, 0, 0]
_CPU2_50 = [600, 0, 0, 1400, 0, 0, 0, 0]

_CPU1_75 = [100, 0, 0, 900, 0, 0, 0, 0]
_CPU2_75 = [850, 0, 0, 1150, 0, 0, 0, 0]

_CPU1_90 = [100, 0, 0, 900, 0, 0, 0, 0]
_CPU2_90 = [1000, 0, 0, 1000, 0, 0, 0, 0]


def _make_output(cpu1, cpu2):
    line1 = f"cpu  {' '.join(str(f) for f in cpu1)}"
    line2 = f"cpu  {' '.join(str(f) for f in cpu2)}"
    return f"{line1}\n{line2}\n"


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(CpuUsageCollectorPlugin, BASE_CFG)


def _latest_status(plugin_id: str = "test-cpu") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-cpu") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestParseCpuLine:
    def test_total_and_idle_correct(self):
        line = "cpu  100 0 50 800 50 0 0 0"
        total, idle = _parse_cpu_line(line)
        assert total == 1000
        assert idle == 850

    def test_short_line_defaults_missing_fields(self):
        line = "cpu  100 0 50 800"
        total, idle = _parse_cpu_line(line)
        assert total == 950
        assert idle == 800


class TestCpuPct:
    def test_50_percent(self):
        l1 = f"cpu  {' '.join(str(f) for f in _CPU1_50)}"
        l2 = f"cpu  {' '.join(str(f) for f in _CPU2_50)}"
        assert _cpu_pct(l1, l2) == pytest.approx(50.0)

    def test_90_percent(self):
        l1 = f"cpu  {' '.join(str(f) for f in _CPU1_90)}"
        l2 = f"cpu  {' '.join(str(f) for f in _CPU2_90)}"
        assert _cpu_pct(l1, l2) == pytest.approx(90.0)

    def test_zero_delta_returns_zero(self):
        line = "cpu  100 0 0 900 0 0 0 0"
        assert _cpu_pct(line, line) == pytest.approx(0.0)

    def test_clamped_to_100(self):
        l1 = "cpu  0 0 0 1000 0 0 0 0"
        l2 = "cpu  2000 0 0 0 0 0 0 0"
        assert _cpu_pct(l1, l2) == pytest.approx(100.0)


class TestLevelFor:
    def test_below_warning_is_online(self):
        assert _level_for(50.0, 70.0, 85.0) == 'online'

    def test_at_warning_is_warning(self):
        assert _level_for(70.0, 70.0, 85.0) == 'warning'

    def test_between_thresholds_is_warning(self):
        assert _level_for(75.0, 70.0, 85.0) == 'warning'

    def test_at_failed_is_failed(self):
        assert _level_for(85.0, 70.0, 85.0) == 'failed'

    def test_above_failed_is_failed(self):
        assert _level_for(95.0, 70.0, 85.0) == 'failed'


class TestCpuUsageCollection:
    async def test_below_warning_sets_online(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _make_output(_CPU1_50, _CPU2_50), ""))
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_above_warning_sets_warning(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _make_output(_CPU1_75, _CPU2_75), ""))
        await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_above_failed_threshold_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _make_output(_CPU1_90, _CPU2_90), ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_cpu_pct_metric_recorded(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _make_output(_CPU1_50, _CPU2_50), ""))
        await plugin.on_collect()
        assert _latest_metric("cpu_pct") == pytest.approx(50.0)

    async def test_ssh_failure_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(-1, "", "timeout"))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_only_one_cpu_sample_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, "cpu  100 0 0 900 0 0 0 0\n", ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_custom_thresholds_respected(self, make_plugin):
        cfg = {**BASE_CFG, "name": "test-cpu-custom", "id": "test-cpu-custom",
               "cpu_warning": 40, "cpu_threshold": 50}
        p = make_plugin(CpuUsageCollectorPlugin, cfg)
        p.ssh_collector.fetch_output = AsyncMock(return_value=(0, _make_output(_CPU1_50, _CPU2_50), ""))
        await p.on_collect()
        assert _latest_status("test-cpu-custom") == "failed"


class TestCpuUsageActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
