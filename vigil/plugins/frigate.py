"""Frigate NVR camera health, from one GET each of /api/stats and /api/config
over HTTP from the Vigil host. Config: api_url (required, Vigil-reachable),
cameras (the subset to watch, default all), min_fps_ratio, api_timeout. A
camera Frigate has disarmed is reported as disarmed and never faults the
monitor; an armed one fails when its capture process is gone or its stream has
stopped, and warns when it delivers less than min_fps_ratio of its configured
detect fps. An unreachable API or malformed payload is failed, and no matching
cameras is a warning pointing at the 'cameras' list."""

import json
from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CollectResult, HttpRequest, HttpResult, Request, Result, Status
)

# Ordered worst-first, so the lowest rank a survey sees is the one worth showing.
_STATE_ORDER = {'stalled': 0, 'degraded': 1, 'disarmed': 2, 'healthy': 3}

_STATE_STATUS = {'stalled': 'failed', 'degraded': 'warning',
                 'disarmed': 'online', 'healthy': 'online'}


def _parse_response(stdout: str) -> Dict[str, Any]:
    """The decoded /api/stats payload, raising ValueError unless it is JSON carrying 'cameras'."""
    try:
        stats = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"stats response was not JSON ({e}): {stdout[:200]!r}") from e
    if not isinstance(stats, dict) or 'cameras' not in stats:
        raise ValueError(f"stats missing 'cameras': {stdout[:200]!r}")
    return stats


def _parse_config(stdout: str) -> Dict[str, Any]:
    """The per-camera section of the decoded /api/config payload, raising ValueError when it is unusable."""
    try:
        config = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"config response was not JSON ({e}): {stdout[:200]!r}") from e
    cameras = config.get('cameras') if isinstance(config, dict) else None
    if not isinstance(cameras, dict):
        raise ValueError(f"config missing 'cameras': {stdout[:200]!r}")
    return cameras


def _http_failure(result) -> Optional[CollectResult]:
    """The failed CollectResult for a transport or HTTP error, or None when the response is usable."""
    if result.error is not None:
        return CollectResult.failed(f"Failed to query Frigate API: {result.error}")
    if result.status_code != 200:
        return CollectResult.failed(f"Frigate API returned HTTP {result.status_code}")
    return None


def _average_inference_ms(detectors: Dict[str, Any]) -> float:
    """Mean detector inference speed in milliseconds, 0.0 when there are no detectors."""
    if not detectors:
        return 0.0
    return sum(d.get('inference_speed', 0) or 0 for d in detectors.values()) / len(detectors)


def _camera_state(camera_data: Dict[str, Any], camera_config: Dict[str, Any],
                  min_fps_ratio: float) -> Tuple[str, float, float]:
    """Classify one camera as disarmed, stalled, degraded or healthy, with its measured and configured fps."""
    fps = float(camera_data.get('camera_fps', 0) or 0)
    target = float((camera_config.get('detect') or {}).get('fps', 0) or 0)
    # Frigate mutates `enabled` as the camera is armed and disarmed over MQTT; enabled_in_config keeps the file value.
    armed = bool(camera_config.get('enabled', True)
                 and camera_config.get('enabled_in_config', True))

    if not armed:
        return 'disarmed', fps, target
    if not camera_data.get('ffmpeg_pid') or fps <= 0:
        return 'stalled', fps, target
    if target > 0 and fps < target * min_fps_ratio:
        return 'degraded', fps, target
    return 'healthy', fps, target


def _survey_cameras(watched: Dict[str, Any], camera_configs: Dict[str, Any],
                    min_fps_ratio: float):
    """Classify every watched camera into per-state counts, the worst state and its camera, fps totals and problem lines."""
    counts = {state: 0 for state in _STATE_ORDER}
    totals = {'camera_fps': 0.0, 'detection_fps': 0.0}
    worst_state = 'healthy'
    worst_camera = None
    problems = []

    for camera_name, camera_data in watched.items():
        state, fps, target = _camera_state(
            camera_data, camera_configs.get(camera_name) or {}, min_fps_ratio)

        counts[state] += 1
        totals['camera_fps'] += fps
        totals['detection_fps'] += float(camera_data.get('detection_fps', 0) or 0)

        if _STATE_ORDER[state] < _STATE_ORDER[worst_state]:
            worst_state = state
            worst_camera = camera_name

        if state == 'stalled':
            problems.append(f"{camera_name}: armed but not streaming ({fps:.1f} fps)")
        elif state == 'degraded':
            problems.append(f"{camera_name}: {fps:.1f} of {target:.0f} fps")

    return counts, worst_state, worst_camera, totals, problems


_DEFAULT_LAYOUT = [
    ['host_card', 'state_card', 'fps_card'],
    ['detector_card', 'armed_card', 'disarmed_card'],
    ['chart'],
    ['events'],
]


class Frigate(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        # Vigil fetches this URL directly (from Vigil's perspective), so it must
        # be an address Vigil can reach — no default; a missing url fails loudly.
        self.api_url = config.get('api_url')
        self.cameras: Optional[List[str]] = config.get('cameras') or None
        self.min_fps_ratio = float(config.get('min_fps_ratio', 0.8))
        self.api_timeout = int(config.get('api_timeout', 10))

    def _get(self, path: str) -> HttpRequest:
        base = self.api_url.rstrip('/')
        return HttpRequest(url=f"{base}{path}", timeout=self.api_timeout)

    def requests(self) -> List[Request]:
        if not self.api_url:
            return []
        return [self._get("/api/stats"), self._get("/api/config")]

    def parse_results(self, results: List[Result]) -> CollectResult:
        """Turns the [stats, config] HTTP results into a CollectResult with
        camera-count and fps metrics, one summary log line, and the worst
        watched camera's state as status — an armed camera that is not
        streaming failed, a degraded one warning, a disarmed one online."""
        if len(results) < 2:
            return CollectResult.failed("No 'api_url' configured")

        stats_result: HttpResult = results[0]
        config_result: HttpResult = results[1]
        for result in (stats_result, config_result):
            failure = _http_failure(result)
            if failure is not None:
                return failure

        try:
            stats = _parse_response(stats_result.text)
            camera_configs = _parse_config(config_result.text)
        except ValueError as e:
            return CollectResult.failed(str(e))

        cameras = stats.get('cameras', {})
        watched = {name: data for name, data in cameras.items()
                   if self.cameras is None or name in self.cameras}

        if not watched:
            return CollectResult.failed(
                "No matching cameras reported by Frigate (check the "
                "'cameras' config list against Frigate's own camera names)",
                level="WARNING", status='warning')

        detectors = stats.get('detectors', {})
        avg_inference = _average_inference_ms(detectors)

        counts, worst_state, worst_camera, totals, problems = _survey_cameras(
            watched, camera_configs, self.min_fps_ratio)
        armed = len(watched) - counts['disarmed']

        metrics = {
            'cameras_total': float(len(watched)),
            'cameras_armed': float(armed),
            'cameras_disarmed': float(counts['disarmed']),
            'cameras_degraded': float(counts['degraded']),
            'cameras_stalled': float(counts['stalled']),
            'camera_fps_total': float(totals['camera_fps']),
            'detection_fps_total': float(totals['detection_fps']),
            'detector_inference_ms': float(avg_inference),
            'worst_camera_rank': float(_STATE_ORDER[worst_state]),
        }

        level = _STATE_STATUS[worst_state]

        parts = [
            f"{len(watched)} camera(s)",
            f"{armed} armed",
            f"{counts['disarmed']} disarmed" if counts['disarmed'] else "",
            f"worst: {worst_camera} ({worst_state})" if worst_camera else "",
            f"{totals['camera_fps']:.1f} fps",
        ]
        if detectors:
            parts.append(f"{avg_inference:.1f}ms inference")
        if problems:
            parts.append("; ".join(problems))

        log_level = Status(level).log_level
        return CollectResult(
            metrics=metrics,
            logs=[(' | '.join(p for p in parts if p), log_level)],
            status=level,
        )

    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'state_card': {
                'metric': 'worst_camera_rank', 'title': 'CAMERAS',
                'format': 'frigate_camera_state', 'color': 'frigate_camera_state_color',
            },
            'fps_card': {
                'metric': 'camera_fps_total', 'title': 'CAMERA FPS', 'format': 'decimal1',
            },
            'detector_card': {
                'metric': 'detector_inference_ms', 'title': 'INFERENCE', 'format': 'ms1',
            },
            'armed_card': {
                'metric': 'cameras_armed', 'title': 'ARMED', 'format': 'int',
            },
            'disarmed_card': {
                'metric': 'cameras_disarmed', 'title': 'DISARMED', 'format': 'int',
            },
        },
        'chart': {'metric': 'camera_fps_total', 'title': 'CAMERA FPS'},
        'events': True,
    }


from vigil.core.ui.spec import register_formatter, register_color_rule

_RANK_TO_LABEL = {0: 'STALLED', 1: 'DEGRADED', 2: 'DISARMED', 3: 'HEALTHY'}


@register_formatter('frigate_camera_state')
def _state_text(v):
    return '--' if v is None else _RANK_TO_LABEL.get(int(v), 'UNKNOWN')


@register_color_rule('frigate_camera_state_color')
def _state_color(v):
    if v is None:
        return None
    rank = int(v)
    if rank == 0:
        return 'failed'
    if rank == 1:
        return 'warning'
    return 'online'
