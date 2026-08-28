"""Frigate NVR camera health, from one GET of /api/stats over HTTP from the
Vigil host. Config: api_url (required, Vigil-reachable), cameras (the subset
to watch, default all), api_timeout. Status follows the worst watched
camera's connection quality — 'poor' is warning and 'unusable' is failed —
with fps, stalls, reconnects, and detector inference speed kept as metrics.
An unreachable API or malformed stats payload is failed, and no matching
cameras is a warning pointing at the 'cameras' list."""

import json
from typing import Any, Dict, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CollectResult, HttpRequest, HttpResult, Request, Result, Status
)

_QUALITY_ORDER = {'unusable': 0, 'poor': 1, 'fair': 2, 'excellent': 3}


def _parse_response(stdout: str) -> Dict[str, Any]:
    try:
        stats = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"stats response was not JSON ({e}): {stdout[:200]!r}") from e
    if not isinstance(stats, dict) or 'cameras' not in stats:
        raise ValueError(f"stats missing 'cameras': {stdout[:200]!r}")
    return stats


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


def _survey_cameras(watched: Dict[str, Any]):
    """Find the worst connection quality across cameras and total their stalls, reconnects, and problem lines."""
    worst_quality = 'excellent'
    worst_camera = None
    total_stalls = 0
    total_reconnects = 0
    problems = []

    for camera_name, camera_data in watched.items():
        quality = camera_data.get('connection_quality', 'unusable')
        stalls = int(camera_data.get('stalls_last_hour', 0) or 0)
        reconnects = int(camera_data.get('reconnects_last_hour', 0) or 0)
        fps = float(camera_data.get('camera_fps', 0) or 0)

        total_stalls += stalls
        total_reconnects += reconnects

        if _QUALITY_ORDER.get(quality, 0) < _QUALITY_ORDER.get(worst_quality, 3):
            worst_quality = quality
            worst_camera = camera_name

        if quality == 'unusable':
            problems.append(f"{camera_name}: unusable ({fps:.1f} fps)")
        elif quality == 'poor':
            problems.append(f"{camera_name}: poor ({fps:.1f} fps)")

    return worst_quality, worst_camera, total_stalls, total_reconnects, problems


def _quality_level(worst_quality: str) -> str:
    """Map the worst camera quality onto a status: unusable failed, poor warning, else online."""
    if worst_quality == 'unusable':
        return 'failed'
    if worst_quality == 'poor':
        return 'warning'
    return 'online'


_DEFAULT_LAYOUT = [
    ['host_card', 'quality_card', 'fps_card'],
    ['detector_card', 'stalls_card', 'reconnects_card'],
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
        self.api_timeout = int(config.get('api_timeout', 10))

    def requests(self) -> List[Request]:
        if not self.api_url:
            return []
        base = self.api_url.rstrip('/')
        return [HttpRequest(url=f"{base}/api/stats", timeout=self.api_timeout)]

    def parse_results(self, results: List[Result]) -> CollectResult:
        """Turns the single /api/stats HTTP result into a CollectResult with
        fps/stall/reconnect/inference metrics, one summary log line, and the
        worst watched camera's connection quality as status (unusable failed,
        poor warning)."""
        if not results:
            return CollectResult.failed("No 'api_url' configured")

        result: HttpResult = results[0]
        failure = _http_failure(result)
        if failure is not None:
            return failure

        try:
            stats = _parse_response(result.text)
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

        worst_quality, worst_camera, total_stalls, total_reconnects, problems = (
            _survey_cameras(watched))

        metrics = {
            'camera_fps_total': float(stats.get('camera_fps', 0) or 0),
            'detection_fps_total': float(stats.get('detection_fps', 0) or 0),
            'detector_inference_ms': float(avg_inference),
            'stalls_last_hour': float(total_stalls),
            'reconnects_last_hour': float(total_reconnects),
            'worst_quality_rank': float(_QUALITY_ORDER.get(worst_quality, 0)),
        }

        level = _quality_level(worst_quality)

        parts = [
            f"{len(watched)} camera(s)",
            f"worst: {worst_camera} ({worst_quality})" if worst_camera else "",
            f"{total_stalls} stalls/h",
            f"{total_reconnects} reconnects/h",
        ]
        if detectors:
            parts.append(f"{avg_inference:.1f}ms inference")
        if problems:
            parts.append("| " + "; ".join(problems))

        log_level = Status(level).log_level
        return CollectResult(
            metrics=metrics,
            logs=[(' | '.join(p for p in parts if p), log_level)],
            status=level,
        )

    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'quality_card': {
                'metric': 'worst_quality_rank', 'title': 'WORST QUALITY',
                'format': 'frigate_quality_rank', 'color': 'frigate_quality_rank_color',
            },
            'fps_card': {
                'metric': 'camera_fps_total', 'title': 'CAMERA FPS', 'format': 'decimal1',
            },
            'detector_card': {
                'metric': 'detector_inference_ms', 'title': 'INFERENCE', 'format': 'ms1',
            },
            'stalls_card': {
                'metric': 'stalls_last_hour', 'title': 'STALLS/H',
                'format': 'int', 'color': 'nonzero_warning',
            },
            'reconnects_card': {
                'metric': 'reconnects_last_hour', 'title': 'RECONNECTS/H',
                'format': 'int', 'color': 'nonzero_warning',
            },
        },
        'chart': {'metric': 'camera_fps_total', 'title': 'CAMERA FPS'},
        'events': True,
    }


from vigil.core.ui.spec import register_formatter, register_color_rule

_RANK_TO_LABEL = {0: 'UNUSABLE', 1: 'POOR', 2: 'FAIR', 3: 'EXCELLENT'}


@register_formatter('frigate_quality_rank')
def _quality_text(v):
    return '--' if v is None else _RANK_TO_LABEL.get(int(v), 'UNKNOWN')


@register_color_rule('frigate_quality_rank_color')
def _quality_rank_color(v):
    if v is None:
        return None
    rank = int(v)
    if rank == 0:
        return 'failed'
    if rank == 1:
        return 'warning'
    return 'online'
