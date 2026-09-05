import json

import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.frigate import Frigate, _parse_config, _parse_response
from vigil.core.connectors.types import HttpRequest, HttpResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-frigate",
    "id":   "test-frigate",
    "api_url": "http://frigate.test:5000",
    "ssh_config": {"host": "test.host"},
}


def _camera_stats(camera_fps=5.0, detection_fps=1.0, ffmpeg_pid=4389):
    """One camera's /api/stats entry, with exactly the keys Frigate 0.17 emits."""
    return {
        "camera_fps": camera_fps, "process_fps": camera_fps, "skipped_fps": 0.0,
        "detection_fps": detection_fps, "detection_enabled": True,
        "pid": 4338, "capture_pid": 4390, "ffmpeg_pid": ffmpeg_pid,
        "audio_rms": 0.0, "audio_dBFS": 0.0,
    }


def _camera_config(enabled=True, enabled_in_config=True, fps=5):
    """One camera's /api/config entry; `enabled` is the live armed state Frigate mutates over MQTT."""
    return {
        "enabled": enabled, "enabled_in_config": enabled_in_config,
        "detect": {"enabled": True, "width": 1280, "height": 720, "fps": fps},
    }


def _stats(cameras=None, detectors=None):
    return {
        "cameras": cameras if cameras is not None else {"front_door": _camera_stats()},
        "detectors": detectors if detectors is not None else {
            "coral": {"inference_speed": 9.5}
        },
        "camera_fps": 5.0, "detection_fps": 1.0,
    }


def _config(cameras=None):
    return {
        "cameras": cameras if cameras is not None else {"front_door": _camera_config()},
    }


def _responder(stats=None, config=None):
    """A fake_run mapping each request to its own endpoint's payload."""
    stats_body = json.dumps(stats if stats is not None else _stats())
    config_body = json.dumps(config if config is not None else _config())

    def run(request):
        body = config_body if request.url.endswith("/api/config") else stats_body
        return HttpResult(status_code=200, text=body)

    return run


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Frigate, BASE_CFG)


def _latest_status(plugin_id: str = "test-frigate") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.plugin_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-frigate") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.plugin_id == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestFixtures:
    def test_stats_fixture_carries_no_invented_fields(self):
        """Frigate reports no connection_quality/stall/reconnect counters, so neither may the fixture."""
        for camera in _stats()["cameras"].values():
            assert "connection_quality" not in camera
            assert "stalls_last_hour" not in camera
            assert "reconnects_last_hour" not in camera


class TestRequests:
    def test_targets_stats_and_config_endpoints(self, plugin):
        reqs = plugin.requests()
        assert [type(r) for r in reqs] == [HttpRequest, HttpRequest]
        assert [r.url for r in reqs] == ["http://frigate.test:5000/api/stats",
                                         "http://frigate.test:5000/api/config"]

    def test_no_url_yields_no_requests(self, make_plugin):
        p = make_plugin(Frigate, {"name": "f", "id": "f",
                                  "ssh_config": {"host": "h"}})
        assert p.requests() == []


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

    def test_parses_camera_configs(self):
        cameras = _parse_config('{"cameras": {"a": {"enabled": false}}}')
        assert cameras["a"]["enabled"] is False

    def test_config_missing_cameras_raises(self):
        with pytest.raises(ValueError, match="missing 'cameras'"):
            _parse_config('{"detectors": {}}')

    def test_malformed_config_raises(self):
        with pytest.raises(ValueError):
            _parse_config('not json')


class TestFrigateCollection:
    async def test_streaming_camera_sets_online(self, plugin, run_requests):
        run_requests(plugin, _responder())
        assert _latest_status() == "online"

    async def test_disarmed_camera_stays_online(self, plugin, run_requests):
        """A camera Home Assistant has disarmed reports no stream by design, and that is not a fault."""
        run_requests(plugin, _responder(
            stats=_stats(cameras={"front_door": _camera_stats(
                camera_fps=0.0, detection_fps=0.0, ffmpeg_pid=0)}),
            config=_config(cameras={"front_door": _camera_config(enabled=False)})))
        assert _latest_status() == "online"
        assert _latest_metric("cameras_disarmed") == 1.0
        assert _latest_metric("cameras_armed") == 0.0
        assert _latest_metric("cameras_stalled") == 0.0

    async def test_armed_camera_without_stream_sets_failed(self, plugin, run_requests):
        run_requests(plugin, _responder(
            stats=_stats(cameras={"front_door": _camera_stats(
                camera_fps=0.0, detection_fps=0.0, ffmpeg_pid=0)})))
        assert _latest_status() == "failed"
        assert _latest_metric("cameras_stalled") == 1.0

    async def test_armed_camera_with_dead_ffmpeg_sets_failed(self, plugin, run_requests):
        run_requests(plugin, _responder(
            stats=_stats(cameras={"front_door": _camera_stats(ffmpeg_pid=None)})))
        assert _latest_status() == "failed"

    async def test_fps_below_ratio_sets_warning(self, plugin, run_requests):
        run_requests(plugin, _responder(
            stats=_stats(cameras={"front_door": _camera_stats(camera_fps=2.0)})))
        assert _latest_status() == "warning"
        assert _latest_metric("cameras_degraded") == 1.0

    async def test_fps_within_ratio_stays_online(self, plugin, run_requests):
        run_requests(plugin, _responder(
            stats=_stats(cameras={"front_door": _camera_stats(camera_fps=4.5)})))
        assert _latest_status() == "online"

    async def test_min_fps_ratio_is_configurable(self, make_plugin, run_requests):
        p = make_plugin(Frigate, {**BASE_CFG, "min_fps_ratio": 0.5})
        run_requests(p, _responder(
            stats=_stats(cameras={"front_door": _camera_stats(camera_fps=3.0)})))
        assert _latest_status() == "online"

    async def test_camera_without_configured_fps_is_not_degraded(self, plugin, run_requests):
        run_requests(plugin, _responder(
            stats=_stats(cameras={"front_door": _camera_stats(camera_fps=1.0)}),
            config=_config(cameras={"front_door": _camera_config(fps=0)})))
        assert _latest_status() == "online"

    async def test_worst_camera_wins(self, plugin, run_requests):
        run_requests(plugin, _responder(
            stats=_stats(cameras={
                "good_cam": _camera_stats(),
                "bad_cam": _camera_stats(camera_fps=0.0, ffmpeg_pid=0),
            }),
            config=_config(cameras={
                "good_cam": _camera_config(),
                "bad_cam": _camera_config(),
            })))
        assert _latest_status() == "failed"

    async def test_disarmed_camera_does_not_mask_a_broken_one(self, plugin, run_requests):
        run_requests(plugin, _responder(
            stats=_stats(cameras={
                "off_cam": _camera_stats(camera_fps=0.0, ffmpeg_pid=0),
                "bad_cam": _camera_stats(camera_fps=0.0, ffmpeg_pid=0),
            }),
            config=_config(cameras={
                "off_cam": _camera_config(enabled=False),
                "bad_cam": _camera_config(),
            })))
        assert _latest_status() == "failed"
        assert _latest_metric("cameras_disarmed") == 1.0
        assert _latest_metric("cameras_stalled") == 1.0

    async def test_camera_disabled_in_config_counts_as_disarmed(self, plugin, run_requests):
        run_requests(plugin, _responder(
            stats=_stats(cameras={"front_door": _camera_stats(
                camera_fps=0.0, ffmpeg_pid=0)}),
            config=_config(cameras={"front_door": _camera_config(
                enabled=False, enabled_in_config=False)})))
        assert _latest_status() == "online"
        assert _latest_metric("cameras_disarmed") == 1.0

    async def test_records_fps_and_inference_metrics(self, plugin, run_requests):
        run_requests(plugin, _responder())
        assert _latest_metric("camera_fps_total") == 5.0
        assert _latest_metric("detection_fps_total") == 1.0
        assert _latest_metric("detector_inference_ms") == 9.5
        assert _latest_metric("cameras_total") == 1.0

    async def test_http_error_sets_failed(self, plugin, run_requests):
        run_requests(plugin, lambda r: HttpResult(
            status_code=None, text="", error="connection refused"))
        assert _latest_status() == "failed"

    async def test_non_200_sets_failed(self, plugin, run_requests):
        run_requests(plugin, lambda r: HttpResult(status_code=502, text=""))
        assert _latest_status() == "failed"

    async def test_config_endpoint_failure_sets_failed(self, plugin, run_requests):
        def run(request):
            if request.url.endswith("/api/config"):
                return HttpResult(status_code=500, text="")
            return HttpResult(status_code=200, text=json.dumps(_stats()))

        run_requests(plugin, run)
        assert _latest_status() == "failed"

    async def test_missing_url_sets_failed(self, make_plugin, run_requests):
        p = make_plugin(Frigate, {"name": "f", "id": "f",
                                  "ssh_config": {"host": "h"}})
        run_requests(p, _responder())
        assert _latest_status("f") == "failed"

    async def test_camera_filter_excludes_others(self, make_plugin, run_requests):
        p = make_plugin(Frigate, {**BASE_CFG, "cameras": ["only_this"]})
        run_requests(p, _responder(
            stats=_stats(cameras={
                "only_this": _camera_stats(),
                "ignored": _camera_stats(camera_fps=0.0, ffmpeg_pid=0),
            }),
            config=_config(cameras={
                "only_this": _camera_config(),
                "ignored": _camera_config(),
            })))
        assert _latest_status("test-frigate") == "online"
        assert _latest_metric("cameras_total", "test-frigate") == 1.0

    async def test_no_matching_cameras_sets_warning(self, make_plugin, run_requests):
        p = make_plugin(Frigate, {**BASE_CFG, "cameras": ["nonexistent"]})
        run_requests(p, _responder())
        assert _latest_status("test-frigate") == "warning"


class TestFrigateActions:
    async def test_on_action_always_returns_none(self, plugin):
        assert plugin.plan_action("anything") is None
