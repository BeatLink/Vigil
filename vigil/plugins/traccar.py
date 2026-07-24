import json
import shlex
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

_AUTH_FAILED = "VIGIL_AUTH_FAILED"


def _build_fetch_script(api_url: str, timeout: int, username: str,
                        password_command: Optional[str], password: Optional[str]) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    if password_command:
        lines.append(f"__pw=$({password_command})")
        auth = f'-u {shlex.quote(username)}:"$__pw"'
    else:
        auth = f'-u {shlex.quote(username + ":" + (password or ""))}'

    lines.append(
        f'__code=$(curl -s -m {timeout} {auth} -o /tmp/.vigil-traccar-$$  '
        f'-w "%{{http_code}}" {shlex.quote(base + "/api/devices")}); '
        f'if [ "$__code" = "401" ]; then echo "{_AUTH_FAILED}" >&2; rm -f /tmp/.vigil-traccar-$$; exit 1; fi; '
        f'cat /tmp/.vigil-traccar-$$; rm -f /tmp/.vigil-traccar-$$'
    )
    return '\n'.join(lines)


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


_DEFAULT_LAYOUT = [
    ['host_card', 'stale_card', 'devices_card'],
    ['chart'],
    ['events'],
]


class TraccarCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.api_url = config.get('api_url', 'http://127.0.0.1:8082')
        self.username = config.get('username')
        self.password = config.get('password')
        self.password_command = config.get('password_command')
        self.stale_warning = float(config.get('stale_warning', 24))
        self.stale_threshold = float(config.get('stale_threshold', 72))
        self.devices: Optional[List[str]] = config.get('devices') or None
        self.api_timeout = int(config.get('api_timeout', 10))

    async def on_collect(self):
        if not self.username:
            self.db_logger.write(
                "No username configured — set username/password_command "
                "for the dedicated Traccar vigil account", level="ERROR")
            self.set_status('failed')
            return

        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.username,
            self.password_command, self.password,
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            if _AUTH_FAILED in stderr:
                self.db_logger.write(
                    "Traccar rejected the configured credentials "
                    "(check username / password_command)", level="ERROR")
            else:
                self.db_logger.write(
                    f"Failed to query Traccar API: {stderr.strip()}", level="ERROR")
            self.set_status('failed')
            return

        try:
            devices = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.set_status('failed')
            return

        watched = [d for d in devices if not d.get('disabled')
                   and (self.devices is None or d.get('name') in self.devices)]

        if not watched:
            self.db_logger.write(
                "No matching enabled devices reported by Traccar", level="WARNING")
            self.set_status('warning')
            return

        self.db_metrics.metric('devices_total', float(len(watched)))

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
            if age >= self.stale_threshold:
                stale_fail.append((name, age))
            elif age >= self.stale_warning:
                stale_warn.append((name, age))

        self.db_metrics.metric('oldest_update_hours', oldest_age)
        self.db_metrics.metric('devices_stale', float(len(stale_warn) + len(stale_fail)))

        level = 'online'
        problems = []

        if stale_fail:
            names = ', '.join(
                f"{n} (never reported)" if a < 0 else f"{n} ({a:.0f}h)"
                for n, a in stale_fail[:3])
            suffix = f" (+{len(stale_fail) - 3} more)" if len(stale_fail) > 3 else ""
            problems.append(f"{len(stale_fail)} stale >= {self.stale_threshold:.0f}h: {names}{suffix}")
            level = 'failed'
        if stale_warn:
            names = ', '.join(f"{n} ({a:.0f}h)" for n, a in stale_warn[:3])
            suffix = f" (+{len(stale_warn) - 3} more)" if len(stale_warn) > 3 else ""
            problems.append(f"{len(stale_warn)} stale >= {self.stale_warning:.0f}h: {names}{suffix}")
            if level == 'online':
                level = 'warning'

        parts = [f"{len(watched)} device(s)", f"oldest update {oldest_age:.0f}h ago"]
        if problems:
            parts.append("| " + "; ".join(problems))

        log_level = "ERROR" if level == 'failed' else "WARNING" if level == 'warning' else "INFO"
        self.db_logger.write(' | '.join(parts), level=log_level)
        self.set_status(level)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class TraccarUIPlugin(UIPlugin):
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

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_color_rule


@register_color_rule('traccar_nonzero_failed')
def _stale_color(v):
    if v is None:
        return None
    return 'failed' if v else 'online'
