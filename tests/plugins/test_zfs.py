import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.zfs import Zfs
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-zfs", "id": "test-zfs", "ssh_config": {"host": "test.host"}}

def _make_zpool(pools: dict) -> str:
    return "".join(f"{name}\t{health}\t{capacity}%\n"
                   for name, (health, capacity) in pools.items())


def _run(plugin, run_cycle, body, code=0, stderr=""):
    return run_cycle(plugin, lambda c: CmdResult(code, body, stderr))



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-zfs"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-zfs") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Zfs, CFG)


from vigil.plugins.zfs import _sanitize_pool


class TestSanitizePool:
    def test_lowercases_and_replaces_separators(self):
        assert _sanitize_pool("Tank-Fast.01") == "tank_fast_01"


class TestCollection:
    async def test_all_online_is_ok(self, plugin, run_cycle):
        _run(plugin, run_cycle, _make_zpool({"pool1": ("ONLINE", 10), "pool2": ("ONLINE", 20)}))
        assert _latest_status() == "online"
        assert _latest_metric("pools_total") == 2
        assert _latest_metric("pools_ok") == 2
        assert _latest_metric("pools_degraded") == 0

    async def test_per_pool_usage_recorded(self, plugin, run_cycle):
        _run(plugin, run_cycle, _make_zpool({"tank": ("ONLINE", 10), "back-up": ("ONLINE", 55)}))
        assert _latest_metric("pool_usage_tank") == pytest.approx(10.0)
        assert _latest_metric("pool_usage_back_up") == pytest.approx(55.0)
        assert _latest_metric("zfs_usage_max") == pytest.approx(55.0)

    @pytest.mark.parametrize("bad_state", ["DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"])
    async def test_all_unhealthy_states_trigger_failed(self, plugin, run_cycle, bad_state):
        _run(plugin, run_cycle, _make_zpool({"pool1": (bad_state, 10)}))
        assert _latest_status() == "failed", f"Expected failed for state {bad_state}"
        assert _latest_metric("pools_degraded") == 1

    async def test_usage_over_warning_warns(self, plugin, run_cycle):
        _run(plugin, run_cycle, _make_zpool({"tank": ("ONLINE", 85)}))
        assert _latest_status() == "warning"

    async def test_usage_at_threshold_fails(self, plugin, run_cycle):
        _run(plugin, run_cycle, _make_zpool({"tank": ("ONLINE", 90)}))
        assert _latest_status() == "failed"

    async def test_custom_thresholds_apply(self, make_plugin, run_cycle):
        p = make_plugin(Zfs, dict(CFG, warning=60, threshold=75))
        _run(p, run_cycle, _make_zpool({"tank": ("ONLINE", 80)}))
        assert _latest_status() == "failed"

    async def test_named_pools_narrow_the_query(self, make_plugin):
        p = make_plugin(Zfs, dict(CFG, pools=['tank', 'backup']))
        assert "name,health,capacity tank backup" in p.commands()[0].text

    async def test_no_pools_sets_offline(self, plugin, run_cycle):
        _run(plugin, run_cycle, "")
        assert _latest_status() == "offline"

    async def test_malformed_lines_skipped(self, plugin, run_cycle):
        _run(plugin, run_cycle, "pool1\tONLINE\t10%\njust_one_word\npool2\tONLINE\t20%\n")
        assert _latest_metric("pools_total") == 2

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        _run(plugin, run_cycle, "", code=-1, stderr="timeout")
        assert _latest_status() == "failed"


class TestUiSpec:
    def test_per_pool_repeat_card_is_its_own_row(self, plugin):
        assert ['zfs_pools'] in plugin.UI_SPEC['layout']
