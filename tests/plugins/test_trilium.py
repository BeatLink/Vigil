import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from vigil.plugins.trilium import Trilium, _age_hours, _parse_response
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

pytestmark = pytest.mark.asyncio


BASE_CFG = {
    "name": "test-trilium",
    "id":   "test-trilium",
    "token": "abc123",
    "stale_warning": 72,
    "ssh_config": {"host": "test.host"},
}


def _iso(hours_ago: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return ts.isoformat().replace('+00:00', 'Z')


def _response(hours_ago=1.0, total_notes=5000):
    return json.dumps({
        "version": {"app": "0.90.4"},
        "database": {"totalNotes": total_notes, "activeNotes": total_notes - 100},
        "statistics": {"lastModified": _iso(hours_ago)},
    })


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Trilium, BASE_CFG)


def _run(plugin, run_cycle, hours_ago=1.0, total_notes=5000):
    payload = _response(hours_ago, total_notes)
    run_cycle(plugin, lambda c: CmdResult(0, payload, ""))


def _latest_status(plugin_id: str = "test-trilium") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-trilium") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestAgeHours:
    def test_recent_timestamp(self):
        assert _age_hours(_iso(3.0)) == pytest.approx(3.0, abs=0.1)

    def test_none_returns_none(self):
        assert _age_hours(None) is None


class TestParseResponse:
    def test_parses_statistics(self):
        data = _parse_response(_response())
        assert "lastModified" in data["statistics"]

    def test_missing_statistics_raises(self):
        with pytest.raises(ValueError, match="statistics"):
            _parse_response('{"foo": "bar"}')


class TestTriliumCollection:
    async def test_recent_modification_sets_online(self, plugin, run_cycle):
        _run(plugin, run_cycle, hours_ago=1.0)
        assert _latest_status() == "online"

    async def test_stale_modification_sets_warning(self, plugin, run_cycle):
        _run(plugin, run_cycle, hours_ago=100.0)
        assert _latest_status() == "warning"

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "connection refused"))
        assert _latest_status() == "failed"

    async def test_bad_token_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, '{"error": "unauthorized"}', ""))
        assert _latest_status() == "failed"

    async def test_records_note_count(self, plugin, run_cycle):
        _run(plugin, run_cycle, total_notes=1234)
        assert _latest_metric("notes_total") == 1234


class TestTriliumActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
