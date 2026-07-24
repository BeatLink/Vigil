import time

import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.push import PushCollectorPlugin
from vigil.collector.orchestration.types import CollectResult
from vigil.core.data.database import db, StatusHistory, Metric


def _latest_status(pid):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == pid
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(pid, name):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == pid) & (Metric.metric_name == name)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


def _cfg(**extra):
    base = {"name": "test-push", "id": "test-push", "interval": 60, "token": "secret"}
    base.update(extra)
    return base


class TestPushCollection:
    async def test_never_pushed_is_failed(self, make_plugin, run_cycle):
        p = make_plugin(PushCollectorPlugin, _cfg())
        run_cycle(p)
        assert _latest_status("test-push") == "failed"

    async def test_recent_push_is_online(self, make_plugin, run_cycle):
        p = make_plugin(PushCollectorPlugin, _cfg(max_age=120))
        p.record_push(status="up")
        run_cycle(p)
        assert _latest_status("test-push") == "online"

    async def test_stale_push_is_failed(self, make_plugin, run_cycle):
        p = make_plugin(PushCollectorPlugin, _cfg(max_age=60))
        p.record_push(status="up")
        p.storage.apply(CollectResult(metrics={"last_push_epoch": time.time() - 120}))
        run_cycle(p)
        assert _latest_status("test-push") == "failed"

    async def test_default_max_age_is_double_interval(self, make_plugin):
        p = make_plugin(PushCollectorPlugin, _cfg(interval=30))
        assert p.max_age == 60

    async def test_reported_down_within_max_age_is_failed(self, make_plugin, run_cycle):
        p = make_plugin(PushCollectorPlugin, _cfg(max_age=120))
        p.record_push(status="down", msg="disk full")
        run_cycle(p)
        assert _latest_status("test-push") == "failed"


class TestRecordPush:
    async def test_record_push_up_sets_online(self, make_plugin):
        p = make_plugin(PushCollectorPlugin, _cfg())
        assert p.record_push(status="up") is True
        assert _latest_status("test-push") == "online"
        assert _latest_metric("test-push", "reported_up") == pytest.approx(1.0)

    async def test_record_push_down_sets_failed(self, make_plugin):
        p = make_plugin(PushCollectorPlugin, _cfg())
        assert p.record_push(status="down") is True
        assert _latest_status("test-push") == "failed"
        assert _latest_metric("test-push", "reported_up") == pytest.approx(0.0)

    async def test_record_push_rejects_invalid_status(self, make_plugin):
        p = make_plugin(PushCollectorPlugin, _cfg())
        assert p.record_push(status="sideways") is False

    async def test_record_push_stores_value(self, make_plugin):
        p = make_plugin(PushCollectorPlugin, _cfg())
        p.record_push(status="up", value=42.5)
        assert _latest_metric("test-push", "value") == pytest.approx(42.5)

    async def test_record_push_without_value_stores_nothing(self, make_plugin):
        p = make_plugin(PushCollectorPlugin, _cfg())
        p.record_push(status="up")
        assert _latest_metric("test-push", "value") is None


class TestPushActions:
    async def test_on_action_always_returns_false(self, make_plugin):
        p = make_plugin(PushCollectorPlugin, _cfg())
        assert p.plan_action("anything") is None
