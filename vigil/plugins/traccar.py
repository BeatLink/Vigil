"""Traccar GPS-device freshness via one GET of /api/devices over HTTP from
the Vigil host, basic-authenticated as a dedicated vigil account. Config:
api_url (required, Vigil-reachable), username, password / password_command,
devices (names to watch, default all enabled), stale_warning /
stale_threshold (hours), api_timeout. A device silent past stale_warning is
warning; one past stale_threshold, or that never reported, is failed, as are
transport and auth errors; no matching enabled devices is a warning."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CollectResult, HttpRequest, HttpResult, Request, Result,
)
from vigil.plugins.base.plugin_helpers import StatusAccumulator, resolve_secret


def _parse_response(stdout: str) -> List[Dict[str, Any]]:
    try:
        devices = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"device list was not JSON ({e}): {stdout[:200]!r}") from e
    if not isinstance(devices, list):
        raise ValueError(f"device list was not a list: {stdout[:200]!r}")
    return devices


def _age_hours(last_update: Optional[str]) -> Optional[float]:
    if not last_update:
        return None
    try:
        ts = datetime.fromisoformat(last_update.replace('Z', '+00:00'))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


def _http_failure(result) -> Optional[CollectResult]:
    """The failed CollectResult for a transport, auth, or HTTP error, or None when the response is usable."""
    if result.error is not None:
        return CollectResult.failed(f"Failed to query Traccar API: {result.error}")
    if result.status_code == 401:
        return CollectResult.failed(
            "Traccar rejected the configured credentials "
            "(check username / password_command)")
    if result.status_code != 200:
        return CollectResult.failed(
            f"Traccar API returned HTTP {result.status_code}")
    return None


def _survey_staleness(watched: List[Dict[str, Any]], stale_warning: float,
                      stale_threshold: float):
    """Bucket devices into warn/fail staleness lists (age -1 meaning never reported) and find the oldest update age."""
    stale_warn: List[Tuple[str, float]] = []
    stale_fail: List[Tuple[str, float]] = []
    oldest_age = 0.0

    for device in watched:
        age = _age_hours(device.get('lastUpdate'))
        name = device.get('name', '?')
        if age is None:
            stale_fail.append((name, -1))
            continue
        oldest_age = max(oldest_age, age)
        if age >= stale_threshold:
            stale_fail.append((name, age))
        elif age >= stale_warning:
            stale_warn.append((name, age))

    return stale_warn, stale_fail, oldest_age


def _accumulate_staleness(stale_warn: List[Tuple[str, float]],
                          stale_fail: List[Tuple[str, float]],
                          stale_warning: float, stale_threshold: float) -> StatusAccumulator:
    """Fold the stale-device buckets into problem lines and a worst-of status."""
    acc = StatusAccumulator()
    if stale_fail:
        names = ', '.join(
            f"{name} (never reported)" if age < 0 else f"{name} ({age:.0f}h)"
            for name, age in stale_fail[:3])
        suffix = f" (+{len(stale_fail) - 3} more)" if len(stale_fail) > 3 else ""
        acc.escalate('failed',
                     f"{len(stale_fail)} stale >= {stale_threshold:.0f}h: {names}{suffix}")
    if stale_warn:
        names = ', '.join(f"{name} ({age:.0f}h)" for name, age in stale_warn[:3])
        suffix = f" (+{len(stale_warn) - 3} more)" if len(stale_warn) > 3 else ""
        acc.escalate('warning',
                     f"{len(stale_warn)} stale >= {stale_warning:.0f}h: {names}{suffix}")
    return acc


_DEFAULT_LAYOUT = [
    ['host_card', 'stale_card', 'devices_card'],
    ['chart'],
    ['events'],
]


class Traccar(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        # Vigil fetches this URL directly; it must be Vigil-reachable, no default.
        self.api_url = config.get('api_url')
        self.username = config.get('username')
        # Resolve the secret once, on the Vigil host, so requests() stays pure.
        self.password = resolve_secret(config.get('password'),
                                       config.get('password_command'))
        self.stale_warning = float(config.get('stale_warning', 24))
        self.stale_threshold = float(config.get('stale_threshold', 72))
        self.devices: Optional[List[str]] = config.get('devices') or None
        self.api_timeout = int(config.get('api_timeout', 10))

    def requests(self) -> List[Request]:
        if not self.api_url or not self.username:
            return []
        base = self.api_url.rstrip('/')
        return [HttpRequest(
            url=f"{base}/api/devices", timeout=self.api_timeout,
            auth=(self.username, self.password or ''),
        )]

    def parse_results(self, results: List[Result]) -> CollectResult:
        """Turns the single /api/devices HTTP result into a CollectResult with
        device-count and staleness metrics, one summary log line, and a status
        where a device past stale_threshold (or never reporting) is failed and
        one past stale_warning is warning."""
        if not self.api_url:
            return CollectResult.failed("No 'api_url' configured")
        if not self.username:
            return CollectResult.failed(
                "No username configured — set username/password_command "
                "for the dedicated Traccar vigil account")

        result: HttpResult = results[0]
        failure = _http_failure(result)
        if failure is not None:
            return failure

        try:
            devices = _parse_response(result.text)
        except ValueError as e:
            return CollectResult.failed(str(e))

        watched = [device for device in devices if not device.get('disabled')
                   and (self.devices is None or device.get('name') in self.devices)]

        if not watched:
            return CollectResult(
                logs=[("No matching enabled devices reported by Traccar", "WARNING")],
                status='warning',
            )

        stale_warn, stale_fail, oldest_age = _survey_staleness(
            watched, self.stale_warning, self.stale_threshold)

        metrics = {
            'devices_total': float(len(watched)),
            'oldest_update_hours': oldest_age,
            'devices_stale': float(len(stale_warn) + len(stale_fail)),
        }

        acc = _accumulate_staleness(stale_warn, stale_fail,
                                    self.stale_warning, self.stale_threshold)

        parts = [f"{len(watched)} device(s)", f"oldest update {oldest_age:.0f}h ago"]
        if acc.problems:
            parts.append("| " + "; ".join(acc.problems))

        return CollectResult(metrics=metrics, logs=[(' | '.join(parts), acc.log_level)],
                             status=acc.status)

    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'stale_card': {
                'metric': 'devices_stale', 'title': 'STALE DEVICES',
                'format': 'int', 'color': 'traccar_nonzero_failed',
            },
            'devices_card': {'metric': 'devices_total', 'title': 'DEVICES', 'format': 'int'},
        },
        'chart': {'metric': 'oldest_update_hours', 'title': 'OLDEST UPDATE (HOURS)'},
        'events': True,
    }


from vigil.core.ui.spec import register_color_rule


@register_color_rule('traccar_nonzero_failed')
def _stale_color(v):
    if v is None:
        return None
    return 'failed' if v else 'online'
