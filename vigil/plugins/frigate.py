import json
from typing import Any, Dict, List, Optional

from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult
from vigil.plugins.base.web_plugin_base import UIPlugin

_QUALITY_ORDER = {'unusable': 0, 'poor': 1, 'fair': 2, 'excellent': 3}


def _build_fetch_script(api_url: str, timeout: int) -> str:
    base = api_url.rstrip('/')
    return f'curl -s -m {timeout} "{base}/api/stats"'


def _parse_response(stdout: str) -> Dict[str, Any]:
    try:
        stats = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"stats response was not JSON ({e}): {stdout[:200]!r}") from e
    if not isinstance(stats, dict) or 'cameras' not in stats:
        raise ValueError(f"stats missing 'cameras': {stdout[:200]!r}")
    return stats


_DEFAULT_LAYOUT = [
    ['host_card', 'quality_card', 'fps_card'],
    ['detector_card', 'stalls_card', 'reconnects_card'],
    ['chart'],
    ['events'],
]


class FrigateCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.api_url = config.get('api_url', 'http://127.0.0.1:5000')
        self.cameras: Optional[List[str]] = config.get('cameras') or None
        self.api_timeout = int(config.get('api_timeout', 10))

    def commands(self) -> List[Command]:
        return [Command(_build_fetch_script(self.api_url, self.api_timeout))]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to query Frigate API: {stderr.strip()}")

        try:
            stats = _parse_response(stdout)
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
        avg_inference = (
            sum(d.get('inference_speed', 0) or 0 for d in detectors.values())
            / len(detectors)
        ) if detectors else 0.0

        metrics = {
            'camera_fps_total': float(stats.get('camera_fps', 0) or 0),
            'detection_fps_total': float(stats.get('detection_fps', 0) or 0),
            'detector_inference_ms': float(avg_inference),
        }

        worst_quality = 'excellent'
        worst_camera = None
        total_stalls = 0
        total_reconnects = 0
        problems = []

        for cam_name, cam_data in watched.items():
            quality = cam_data.get('connection_quality', 'unusable')
            stalls = int(cam_data.get('stalls_last_hour', 0) or 0)
            reconnects = int(cam_data.get('reconnects_last_hour', 0) or 0)
            fps = float(cam_data.get('camera_fps', 0) or 0)

            total_stalls += stalls
            total_reconnects += reconnects

            if _QUALITY_ORDER.get(quality, 0) < _QUALITY_ORDER.get(worst_quality, 3):
                worst_quality = quality
                worst_camera = cam_name

            if quality == 'unusable':
                problems.append(f"{cam_name}: unusable ({fps:.1f} fps)")
            elif quality == 'poor':
                problems.append(f"{cam_name}: poor ({fps:.1f} fps)")

        metrics['stalls_last_hour'] = float(total_stalls)
        metrics['reconnects_last_hour'] = float(total_reconnects)
        metrics['worst_quality_rank'] = float(_QUALITY_ORDER.get(worst_quality, 0))

        if worst_quality == 'unusable':
            level = 'failed'
        elif worst_quality == 'poor':
            level = 'warning'
        else:
            level = 'online'

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

        log_level = "ERROR" if level == 'failed' else "WARNING" if level == 'warning' else "INFO"
        return CollectResult(
            metrics=metrics,
            logs=[(' | '.join(p for p in parts if p), log_level)],
            status=level,
        )


from vigil.core.ui.ui.spec import generic_render, register_formatter, register_color_rule

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


class FrigateUIPlugin(UIPlugin):
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

    def render_ui(self, context: str = 'page'):
        generic_render(self, context)
