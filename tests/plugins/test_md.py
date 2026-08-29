import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.md import Md
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-md", "id": "test-md", "ssh_config": {"host": "test.host"}}

_MDSTAT_CLEAN = """Personalities : [raid1]
md0 : active raid1 sdb1[1] sda1[0]
      1953382464 blocks super 1.2 [2/2] [UU]

unused devices: <none>
"""

_MDSTAT_DEGRADED = """Personalities : [raid1]
md0 : active raid1 sda1[0]
      1953382464 blocks super 1.2 [2/1] [U_]

unused devices: <none>
"""

_MDSTAT_RECOVERING = """Personalities : [raid1]
md0 : active raid1 sdb1[1] sda1[0]
      1953382464 blocks super 1.2 [2/2] [UU]
      [====>................]  recovery = 22.6% (442k/1953k) finish=30.0min speed=100000K/sec

unused devices: <none>
"""

_MDSTAT_EMPTY = """Personalities :
unused devices: <none>
"""


def _run(plugin, run_cycle, body, code=0, stderr=""):
    return run_cycle(plugin, lambda c: CmdResult(code, body, stderr))



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == "test-md"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == "test-md") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Md, CFG)


class TestCollection:
    async def test_clean_arrays_are_online(self, plugin, run_cycle):
        _run(plugin, run_cycle, _MDSTAT_CLEAN)
        assert _latest_status() == "online"
        assert _latest_metric("arrays_total") == pytest.approx(1.0)
        assert _latest_metric("arrays_ok") == pytest.approx(1.0)
        assert _latest_metric("arrays_degraded") == pytest.approx(0.0)

    async def test_a_degraded_array_fails(self, plugin, run_cycle):
        _run(plugin, run_cycle, _MDSTAT_DEGRADED)
        assert _latest_status() == "failed"
        assert _latest_metric("arrays_degraded") == pytest.approx(1.0)

    async def test_a_rebuilding_array_warns(self, plugin, run_cycle):
        _run(plugin, run_cycle, _MDSTAT_RECOVERING)
        assert _latest_status() == "warning"
        assert _latest_metric("arrays_ok") == pytest.approx(1.0)

    async def test_a_host_with_no_arrays_is_offline(self, plugin, run_cycle):
        _run(plugin, run_cycle, _MDSTAT_EMPTY)
        assert _latest_status() == "offline"

    async def test_an_unreadable_mdstat_fails(self, plugin, run_cycle):
        _run(plugin, run_cycle, "", code=1, stderr="no such file")
        assert _latest_status() == "failed"

    async def test_it_contributes_its_cards(self, plugin):
        assert {'md_total_card', 'md_ok_card', 'md_degraded_card'} <= set(plugin.UI_SPEC['cards'])
