import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.temperature import TemperatureCollectorPlugin, _level_for
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-temp",
    "id":   "test-temp",
    "temp_warning":   70,
    "temp_threshold": 80,
    "ssh_config": {"host": "test.host"},
}

_TEMPS_ONLINE  = [42_000, 38_000, 45_000]
_TEMPS_WARNING = [72_000, 68_000]
_TEMPS_FAILED  = [85_000, 90_000]


def _make_output(temps_mc):
    return "".join(f"SENSOR:x86_pkg_temp_{i}:{t}\n" for i, t in enumerate(temps_mc))


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(TemperatureCollectorPlugin, BASE_CFG)


def _latest_status(plugin_id: str = "test-temp") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-temp") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestLevelFor:
    def test_below_warning_is_online(self):
        assert _level_for(45.0, 70.0, 80.0) == 'online'

    def test_at_warning_is_warning(self):
        assert _level_for(70.0, 70.0, 80.0) == 'warning'

    def test_between_thresholds_is_warning(self):
        assert _level_for(72.0, 70.0, 80.0) == 'warning'

    def test_at_failed_is_failed(self):
        assert _level_for(80.0, 70.0, 80.0) == 'failed'

    def test_above_failed_is_failed(self):
        assert _level_for(90.0, 70.0, 80.0) == 'failed'


class TestTemperatureCollection:
    async def test_below_warning_sets_online(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _make_output(_TEMPS_ONLINE), ""))
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_above_warning_sets_warning(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _make_output(_TEMPS_WARNING), ""))
        await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_above_threshold_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _make_output(_TEMPS_FAILED), ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_temp_c_metric_recorded(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _make_output(_TEMPS_ONLINE), ""))
        await plugin.on_collect()
        assert _latest_metric("temp_c") == pytest.approx(45.0)

    async def test_takes_maximum_zone(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _make_output([30_000, 75_000, 60_000]), ""))
        await plugin.on_collect()
        assert _latest_metric("temp_c") == pytest.approx(75.0)

    async def test_no_thermal_zones_sets_online(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, "", ""))
        await plugin.on_collect()
        assert _latest_status() == "online"
        assert _latest_metric("temp_c") is None

    async def test_no_thermal_zones_does_not_store_metric(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, "", ""))
        await plugin.on_collect()
        assert _latest_metric("temp_c") is None

    async def test_ssh_failure_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(-1, "", "timeout"))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_custom_thresholds_respected(self, make_plugin):
        cfg = {**BASE_CFG, "name": "test-temp-custom", "id": "test-temp-custom",
               "temp_warning": 30, "temp_threshold": 40}
        p = make_plugin(TemperatureCollectorPlugin, cfg)
        p.ssh_collector.fetch_output = AsyncMock(
            return_value=(0, _make_output(_TEMPS_ONLINE), ""))
        await p.on_collect()
        assert _latest_status("test-temp-custom") == "failed"


class TestTemperatureActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
