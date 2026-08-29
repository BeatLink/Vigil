import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.temperature import Temperature
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-temperature", "id": "test-temperature", "ssh_config": {"host": "test.host"}}

def _sensors(*temps_mc):
    return "".join(f"SENSOR:x86_pkg_temp_{i}:{t}\n" for i, t in enumerate(temps_mc))



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == "test-temperature"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == "test-temperature") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Temperature, CFG)


class TestCollection:
    async def test_hottest_zone_is_the_status_and_every_zone_is_kept(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _sensors(42_000, 55_000), ""))
        assert _latest_status() == "online"
        assert _latest_metric("temp_c") == pytest.approx(55.0)
        assert _latest_metric("temp_zone_x86_pkg_temp_1") == pytest.approx(55.0)

    async def test_thresholds_are_configurable(self, make_plugin, run_cycle):
        p = make_plugin(Temperature, dict(CFG, warning=50, threshold=60))
        run_cycle(p, lambda c: CmdResult(0, _sensors(52_000), ""))
        assert _latest_status() == "warning"
        run_cycle(p, lambda c: CmdResult(0, _sensors(65_000), ""))
        assert _latest_status() == "failed"

    async def test_a_host_with_no_zones_stays_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _sensors(), ""))
        assert _latest_status() == "online"
        assert _latest_metric("temp_c") is None


class TestUiSpec:
    def test_zone_cards_get_their_own_row(self, plugin):
        assert ['sensors'] in plugin.UI_SPEC['layout']
