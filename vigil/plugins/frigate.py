"""
Frigate NVR camera health via the internal stats API.

Complements a `systemd_service` monitor on frigate rather than replacing it.
That one answers "is the process alive"; this one answers "is each camera
actually producing usable frames", which is a different failure. The case
that motivates it: the Frigate process stays up while a camera's stream
wedges — a USB webcam that enumerates but delivers only truncated frames (see
the ExecStartPre replug workaround in frigate.nix), a dead RTSP link, ffmpeg
stuck retrying — and every liveness check stays green throughout. The service
is up, the API answers, and the only visible symptom is a camera producing
no frames or none worth detecting on.

Frigate itself computes the signal worth alerting on: `connection_quality`,
a precomputed enum (`excellent` / `fair` / `poor` / `unusable`) derived from
camera_fps, reconnects, and stalls. This plugin surfaces that directly rather
than re-deriving thresholds from the raw counters, so it tracks whatever
Frigate's own definition of "unusable" is as that logic evolves upstream.

Read over SSH from `http://127.0.0.1:5000`, Frigate's documented internal
port for trusted local integrations: any request arriving there is
automatically treated as an authenticated admin, by design, specifically so
tools like this need no credential and no change to the real auth setup used
by actual users on the regular (8971-mapped) port. This is Frigate's own
recommended mechanism, not a workaround — see docs.frigate.video and
`frigate/api/auth.py`'s handling of the internal port.

Config options:
  api_url          Base URL of Frigate's internal (unauthenticated) API, as
                   seen from the monitored host (default:
                   http://127.0.0.1:5000)
  cameras          Camera names to judge. Empty (default) means every camera
                   Frigate reports. Explicit list lets a camera known to be
                   disabled or torn down stay out of the check entirely
                   rather than needing per-camera enabled/disabled logic here.
  api_timeout      Seconds allowed for the remote curl call (default: 10)
"""
import json
from typing import Any, Dict, List, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

# Frigate's own precomputed verdict, ordered worst-to-best is the reverse of
# this — used to find the single worst camera to headline the log line.
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
    """Monitors Frigate camera health via the internal /api/stats endpoint."""

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.api_url = config.get('api_url', 'http://127.0.0.1:5000')
        self.cameras: Optional[List[str]] = config.get('cameras') or None
        self.api_timeout = int(config.get('api_timeout', 10))

    async def on_collect(self):
        script = _build_fetch_script(self.api_url, self.api_timeout)
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            self.db_logger.write(f"Failed to query Frigate API: {stderr.strip()}", level="ERROR")
            self.set_status('failed')
            return

        try:
            stats = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.set_status('failed')
            return

        cameras = stats.get('cameras', {})
        watched = {name: data for name, data in cameras.items()
                   if self.cameras is None or name in self.cameras}

        if not watched:
            self.db_logger.write(
                "No matching cameras reported by Frigate (check the "
                "'cameras' config list against Frigate's own camera names)",
                level="WARNING")
            self.set_status('warning')
            return

        detectors = stats.get('detectors', {})
        avg_inference = (
            sum(d.get('inference_speed', 0) or 0 for d in detectors.values())
            / len(detectors)
        ) if detectors else 0.0

        self.db_metrics.metric('camera_fps_total', float(stats.get('camera_fps', 0) or 0))
        self.db_metrics.metric('detection_fps_total', float(stats.get('detection_fps', 0) or 0))
        self.db_metrics.metric('detector_inference_ms', float(avg_inference))

        # --- status ---------------------------------------------------------
        # Judged per camera, then the worst camera's quality decides the
        # overall status — one dead camera must not be diluted into "fine on
        # average" by others that are healthy.
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

        self.db_metrics.metric('stalls_last_hour', float(total_stalls))
        self.db_metrics.metric('reconnects_last_hour', float(total_reconnects))
        self.db_metrics.metric(
            'worst_quality_rank', float(_QUALITY_ORDER.get(worst_quality, 0)))

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
        self.db_logger.write(' | '.join(p for p in parts if p), level=log_level)
        self.set_status(level)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class FrigateUIPlugin(UIPlugin):
    """Dashboard rendering for the frigate monitor."""

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart, on_data_event
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('quality_card'):
            quality_label = info_card('WORST QUALITY', '--')
        with layout.cell('fps_card'):
            fps_label = info_card('CAMERA FPS', '--')
        with layout.cell('detector_card'):
            detector_label = info_card('INFERENCE', '--')
        with layout.cell('stalls_card'):
            stalls_label = info_card('STALLS/H', '--')
        with layout.cell('reconnects_card'):
            reconnects_label = info_card('RECONNECTS/H', '--')
        with layout.cell('chart'):
            history_chart('CAMERA FPS', self.id, 'camera_fps_total')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        _rank_to_label = {0: 'UNUSABLE', 1: 'POOR', 2: 'FAIR', 3: 'EXCELLENT'}

        def update_cards():
            rank    = self.latest_metric('worst_quality_rank')
            fps     = self.latest_metric('camera_fps_total')
            infer   = self.latest_metric('detector_inference_ms')
            stalls  = self.latest_metric('stalls_last_hour')
            reconn  = self.latest_metric('reconnects_last_hour')

            if rank is not None:
                label = _rank_to_label.get(int(rank.value), 'UNKNOWN')
                quality_label.text = label
                colour = (STATUS_COLORS['failed'] if rank.value == 0
                          else STATUS_COLORS['warning'] if rank.value == 1
                          else STATUS_COLORS['online'])
                quality_label.style(f'color: {colour}')
            if fps:
                fps_label.text = f'{fps.value:.1f}'
            if infer:
                detector_label.text = f'{infer.value:.1f} ms'
            if stalls:
                stalls_label.text = f'{int(stalls.value)}'
                stalls_label.style(
                    f'color: {STATUS_COLORS["warning" if stalls.value > 0 else "online"]}')
            if reconn:
                reconnects_label.text = f'{int(reconn.value)}'
                reconnects_label.style(
                    f'color: {STATUS_COLORS["warning" if reconn.value > 0 else "online"]}')

        on_data_event('metric', quality_label, update_cards)
