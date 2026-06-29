import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.load_average import LoadAveragePlugin, _level_for
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-load",
    "id":   "test-load",
    "ssh_config": {"host": "test.host"},
}

CFG_WITH_THRESHOLDS = {
    **BASE_CFG,
    "name": "test-load-thresh",
    "id":   "test-load-thresh",
    "load_warning":   70.0,
    "load_threshold": 100.0,
}

# Raw load tuples (1m, 5m, 15m) — tests use cpus=4
# Percentages = raw / 4 * 100
_LOAD_OK      = (1.2, 1.0, 0.8)   # 1m → 30%  → online  (below 70%)
_LOAD_WARNING = (3.2, 2.8, 2.4)   # 1m → 80%  → warning (between 70% and 100%)
_LOAD_FAILED  = (5.0, 4.8, 4.6)   # 1m → 125% → failed  (above 100%)


def _make_output(load, cpus=4):
    """load: (1m, 5m, 15m) tuple; cpus: int"""
    return (
        f"LOAD:{load[0]} {load[1]} {load[2]} 1/100 12345\n"
        f"CPUS:{cpus}\n"
    )


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(LoadAveragePlugin, BASE_CFG)


@pytest.fixture
def thresh_plugin(make_plugin):
    return make_plugin(LoadAveragePlugin, CFG_WITH_THRESHOLDS)


def _latest_status(plugin_id: str = "test-load") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-load") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestLevelFor:
    def test_below_warning_is_online(self):
        assert _level_for(50.0, 70.0, 100.0) == 'online'

    def test_at_warning_is_warning(self):
        assert _level_for(70.0, 70.0, 100.0) == 'warning'

    def test_between_thresholds_is_warning(self):
        assert _level_for(80.0, 70.0, 100.0) == 'warning'

    def test_at_failed_is_failed(self):
        assert _level_for(100.0, 70.0, 100.0) == 'failed'

    def test_above_failed_is_failed(self):
        assert _level_for(125.0, 70.0, 100.0) == 'failed'


class TestLoadAverageCollection:
    async def test_load_pct_metrics_recorded(self, plugin):
        # (1.2 / 4) * 100 = 30%, (1.0 / 4) * 100 = 25%, (0.8 / 4) * 100 = 20%
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _make_output(_LOAD_OK, cpus=4), ""))
        await plugin.on_collect()
        assert _latest_metric("load_pct_1m")  == pytest.approx(30.0)
        assert _latest_metric("load_pct_5m")  == pytest.approx(25.0)
        assert _latest_metric("load_pct_15m") == pytest.approx(20.0)

    async def test_pct_scales_with_core_count(self, plugin):
        # Same raw load, 8 cores → half the percentage
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _make_output((2.0, 2.0, 2.0), cpus=8), ""))
        await plugin.on_collect()
        assert _latest_metric("load_pct_1m") == pytest.approx(25.0)

    async def test_no_thresholds_always_online(self, plugin):
        # BASE_CFG has no thresholds — any load stays online
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _make_output(_LOAD_FAILED, cpus=4), ""))
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_below_warning_sets_online(self, thresh_plugin):
        thresh_plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _make_output(_LOAD_OK, cpus=4), ""))
        await thresh_plugin.on_collect()
        assert _latest_status("test-load-thresh") == "online"

    async def test_above_warning_sets_warning(self, thresh_plugin):
        thresh_plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _make_output(_LOAD_WARNING, cpus=4), ""))
        await thresh_plugin.on_collect()
        assert _latest_status("test-load-thresh") == "warning"

    async def test_above_failed_threshold_sets_failed(self, thresh_plugin):
        thresh_plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _make_output(_LOAD_FAILED, cpus=4), ""))
        await thresh_plugin.on_collect()
        assert _latest_status("test-load-thresh") == "failed"

    async def test_missing_cpus_line_falls_back_to_1(self, plugin):
        # Without CPUS: line the raw load is used as-is (core count = 1)
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, "LOAD:2.0 1.5 1.0 1/100 12345\n", ""))
        await plugin.on_collect()
        assert _latest_metric("load_pct_1m") == pytest.approx(200.0)

    async def test_missing_load_line_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, "CPUS:4\n", ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_ssh_failure_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(-1, "", "timeout"))
        await plugin.on_collect()
        assert _latest_status() == "failed"


class TestLoadAverageActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
