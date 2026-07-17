import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.raid import RaidPlugin
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {"name": "test-raid", "id": "test-raid", "ssh_config": {"host": "test.host"}}


def _latest_status(pid="test-raid"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric, name="test-raid"):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


_CLEAN = """Personalities : [raid1]
md0 : active raid1 sdb1[1] sda1[0]
      1953382464 blocks super 1.2 [2/2] [UU]

unused devices: <none>
"""

_DEGRADED = """Personalities : [raid1]
md0 : active raid1 sda1[0]
      1953382464 blocks super 1.2 [2/1] [U_]

unused devices: <none>
"""

_RECOVERING = """Personalities : [raid1]
md0 : active raid1 sdb1[1] sda1[0]
      1953382464 blocks super 1.2 [2/2] [UU]
      [====>................]  recovery = 22.6% (442k/1953k) finish=30.0min speed=100000K/sec

unused devices: <none>
"""

_EMPTY = """Personalities :
unused devices: <none>
"""


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(RaidPlugin, BASE_CFG)


class TestRaidCollection:
    async def test_clean_online(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _CLEAN, ""))
        await plugin.on_collect()
        assert _latest_status() == "online"
        assert _latest_metric("arrays_total") == pytest.approx(1.0)
        assert _latest_metric("arrays_degraded") == pytest.approx(0.0)

    async def test_degraded_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _DEGRADED, ""))
        await plugin.on_collect()
        assert _latest_status() == "failed"
        assert _latest_metric("arrays_degraded") == pytest.approx(1.0)

    async def test_recovering_warning(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _RECOVERING, ""))
        await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_no_arrays_offline(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(0, _EMPTY, ""))
        await plugin.on_collect()
        assert _latest_status() == "offline"

    async def test_read_failure_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(1, "", "no such file"))
        await plugin.on_collect()
        assert _latest_status() == "failed"


class TestRaidActions:
    async def test_on_action_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
