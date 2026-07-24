import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from vigil.plugins.traccar import Traccar, _age_hours, _AUTH_FAILED
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

pytestmark = pytest.mark.asyncio


BASE_CFG = {
    "name": "test-traccar",
    "id":   "test-traccar",
    "username": "vigil",
    "password": "hunter2",
    "stale_warning": 24,
    "stale_threshold": 72,
    "ssh_config": {"host": "test.host"},
}


def _iso(hours_ago: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return ts.isoformat().replace('+00:00', 'Z')


def _device(name="Phone", hours_ago=1.0, disabled=False):
    return {"name": name, "disabled": disabled, "lastUpdate": _iso(hours_ago)}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Traccar, BASE_CFG)


def _run(plugin, run_cycle, devices=None):
    payload = json.dumps(devices if devices is not None else [_device()])
    run_cycle(plugin, lambda c: CmdResult(0, payload, ""))


def _latest_status(plugin_id: str = "test-traccar") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-traccar") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestAgeHours:
    def test_recent_timestamp(self):
        age = _age_hours(_iso(2.0))
        assert age == pytest.approx(2.0, abs=0.1)

    def test_none_returns_none(self):
        assert _age_hours(None) is None

    def test_unparseable_returns_none(self):
        assert _age_hours("not-a-date") is None


class TestTraccarCollection:
    async def test_fresh_device_sets_online(self, plugin, run_cycle):
        _run(plugin, run_cycle, [_device(hours_ago=1.0)])
        assert _latest_status() == "online"

    async def test_stale_device_sets_warning(self, plugin, run_cycle):
        _run(plugin, run_cycle, [_device(hours_ago=30.0)])
        assert _latest_status() == "warning"

    async def test_very_stale_device_sets_failed(self, plugin, run_cycle):
        _run(plugin, run_cycle, [_device(hours_ago=100.0)])
        assert _latest_status() == "failed"

    async def test_disabled_devices_excluded(self, plugin, run_cycle):
        _run(plugin, run_cycle, [_device(hours_ago=200.0, disabled=True)])
        assert _latest_status() == "warning"

    async def test_auth_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", _AUTH_FAILED))
        assert _latest_status() == "failed"

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "connection refused"))
        assert _latest_status() == "failed"

    async def test_missing_username_sets_failed(self, make_plugin, run_cycle):
        cfg = {k: v for k, v in BASE_CFG.items() if k != "username"}
        p = make_plugin(Traccar, cfg)
        run_cycle(p, lambda c: CmdResult(0, "", ""))
        assert _latest_status("test-traccar") == "failed"

    async def test_never_reported_counts_as_stale(self, plugin, run_cycle):
        _run(plugin, run_cycle, [{"name": "NoFix", "disabled": False, "lastUpdate": None}])
        assert _latest_status() == "failed"

    async def test_device_filter_excludes_others(self, make_plugin, run_cycle):
        p = make_plugin(Traccar, {**BASE_CFG, "devices": ["Phone"]})
        _run(p, run_cycle, [_device(name="Phone", hours_ago=1.0),
                            _device(name="OldTablet", hours_ago=500.0)])
        assert _latest_status("test-traccar") == "online"


class TestTraccarActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
