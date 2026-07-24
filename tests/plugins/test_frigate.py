import json

import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.frigate import Frigate, _build_fetch_script, _parse_response
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-frigate",
    "id":   "test-frigate",
    "ssh_config": {"host": "test.host"},
}


def _stats(cameras=None, detectors=None):
    return {
        "cameras": cameras if cameras is not None else {
            "front_door": {
                "camera_fps": 5.0, "connection_quality": "excellent",
                "stalls_last_hour": 0, "reconnects_last_hour": 0,
            }
        },
        "detectors": detectors if detectors is not None else {
            "coral": {"inference_speed": 9.5}
        },
        "camera_fps": 5.0, "detection_fps": 1.0,
    }


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Frigate, BASE_CFG)


def _result(stats=None):
    return CmdResult(0, json.dumps(stats if stats is not None else _stats()), "")


def _latest_status(plugin_id: str = "test-frigate") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-frigate") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestBuildFetchScript:
    def test_targets_stats_endpoint(self):
        script = _build_fetch_script("http://127.0.0.1:5000", 10)
        assert "/api/stats" in script


class TestParseResponse:
    def test_parses_cameras(self):
        stats = _parse_response('{"cameras": {"a": {}}}')
        assert "a" in stats["cameras"]

    def test_missing_cameras_raises(self):
        with pytest.raises(ValueError, match="missing 'cameras'"):
            _parse_response('{"foo": "bar"}')

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError):
            _parse_response('not json')


class TestFrigateCollection:
    async def test_excellent_quality_sets_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _result())
        assert _latest_status() == "online"

    async def test_unusable_quality_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _result(_stats(cameras={
            "front_door": {"camera_fps": 0.0, "connection_quality": "unusable",
                           "stalls_last_hour": 5, "reconnects_last_hour": 12}
        })))
        assert _latest_status() == "failed"

    async def test_poor_quality_sets_warning(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _result(_stats(cameras={
            "front_door": {"camera_fps": 1.0, "connection_quality": "poor",
                           "stalls_last_hour": 2, "reconnects_last_hour": 1}
        })))
        assert _latest_status() == "warning"

    async def test_worst_camera_wins(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _result(_stats(cameras={
            "good_cam": {"camera_fps": 5.0, "connection_quality": "excellent",
                        "stalls_last_hour": 0, "reconnects_last_hour": 0},
            "bad_cam": {"camera_fps": 0.0, "connection_quality": "unusable",
                       "stalls_last_hour": 10, "reconnects_last_hour": 20},
        })))
        assert _latest_status() == "failed"

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "connection refused"))
        assert _latest_status() == "failed"

    async def test_camera_filter_excludes_others(self, make_plugin, run_cycle):
        p = make_plugin(Frigate, {**BASE_CFG, "cameras": ["only_this"]})
        run_cycle(p, lambda c: _result(_stats(cameras={
            "only_this": {"camera_fps": 5.0, "connection_quality": "excellent",
                         "stalls_last_hour": 0, "reconnects_last_hour": 0},
            "ignored": {"camera_fps": 0.0, "connection_quality": "unusable",
                       "stalls_last_hour": 100, "reconnects_last_hour": 100},
        })))
        assert _latest_status("test-frigate") == "online"

    async def test_no_matching_cameras_sets_warning(self, make_plugin, run_cycle):
        p = make_plugin(Frigate, {**BASE_CFG, "cameras": ["nonexistent"]})
        run_cycle(p, lambda c: _result())
        assert _latest_status("test-frigate") == "warning"


class TestFrigateActions:
    async def test_on_action_always_returns_none(self, plugin):
        assert plugin.plan_action("anything") is None
