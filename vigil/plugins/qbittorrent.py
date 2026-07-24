import json
import shlex
from typing import Any, Dict, List, Optional, Tuple, Union

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import ActionPlan, CmdResult, Command, CollectResult

_SEP = "@@VIGIL_SPLIT@@"

_AUTH_FAILED = "VIGIL_AUTH_FAILED"

_STALLED_STATES = {'stalledDL', 'metaDL', 'stalledUP'}

_ERROR_STATES = {'error', 'missingFiles'}

_ACTIVE_DL_STATES = {'downloading', 'metaDL', 'stalledDL', 'queuedDL',
                     'forcedDL', 'checkingDL', 'allocating', 'downloadingMetadata'}


def _auth_preamble(base: str, timeout: int, password_command: Optional[str],
                   username: Optional[str], password: Optional[str]) -> Tuple[List[str], str]:
    if not (username and (password_command or password)):
        return [], ''

    lines = []
    if password_command:
        lines.append(f"__pw=$({password_command})")
    else:
        lines.append(f"__pw={shlex.quote(password)}")

    lines.append('__jar=$(mktemp)')
    lines.append("""trap 'rm -f "$__jar"' EXIT INT TERM""")
    lines.append(
        f'__login=$(curl -s -m {timeout} -c "$__jar" '
        f'-H {shlex.quote("Referer: " + base)} '
        f'--data-urlencode {shlex.quote("username=" + username)} '
        f'--data-urlencode "password=$__pw" '
        f'{shlex.quote(base + "/api/v2/auth/login")})'
    )
    lines.append(
        f'case "$__login" in *Ok.*) ;; *) '
        f'echo "{_AUTH_FAILED}: $__login" >&2; exit 1 ;; esac'
    )
    return lines, '-b "$__jar"'


def _build_fetch_script(api_url: str, timeout: int, password_command: Optional[str],
                        username: Optional[str], password: Optional[str]) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    auth_lines, auth = _auth_preamble(base, timeout, password_command, username, password)
    lines.extend(auth_lines)

    lines.append(f'curl -s -m {timeout} {auth} {shlex.quote(base + "/api/v2/transfer/info")}')
    lines.append(f'echo "{_SEP}"')
    lines.append(f'curl -s -m {timeout} {auth} {shlex.quote(base + "/api/v2/torrents/info")}')
    return '\n'.join(lines)


def _action_curl_line(base: str, timeout: int, auth: str, endpoint: str,
                      params: Optional[Dict[str, str]] = None) -> str:
    parts = [
        f'curl -s -f -m {timeout} {auth}',
        f'-H {shlex.quote("Referer: " + base)}',
    ]
    for key, value in (params or {}).items():
        parts.append(f'--data-urlencode {shlex.quote(f"{key}={value}")}')
    parts.append(shlex.quote(base + endpoint))
    return ' '.join(parts)


def _build_action_script(api_url: str, timeout: int, password_command: Optional[str],
                         username: Optional[str], password: Optional[str],
                         endpoint: str, params: Optional[Dict[str, str]] = None) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    auth_lines, auth = _auth_preamble(base, timeout, password_command, username, password)
    lines.extend(auth_lines)
    lines.append(_action_curl_line(base, timeout, auth, endpoint, params))
    return '\n'.join(lines)


def _build_fallback_action_script(api_url: str, timeout: int, password_command: Optional[str],
                                  username: Optional[str], password: Optional[str],
                                  modern_endpoint: str, legacy_endpoint: str,
                                  params: Optional[Dict[str, str]] = None) -> str:
    """Try the modern endpoint; on failure fall back to the legacy one (older
    qBittorrent versions use start/stop instead of resume/pause)."""
    base = api_url.rstrip('/')
    lines = ["set -e"]

    auth_lines, auth = _auth_preamble(base, timeout, password_command, username, password)
    lines.extend(auth_lines)

    modern_line = _action_curl_line(base, timeout, auth, modern_endpoint, params)
    legacy_line = _action_curl_line(base, timeout, auth, legacy_endpoint, params)
    lines.append(f'{modern_line} || {legacy_line}')
    return '\n'.join(lines)


def _build_recheck_script(api_url: str, timeout: int, password_command: Optional[str],
                          username: Optional[str], password: Optional[str]) -> str:
    """Fetch the torrent list, extract hashes of errored torrents, and (if
    any) issue a recheck for them — all in one remote round trip."""
    base = api_url.rstrip('/')
    lines = ["set -e"]

    auth_lines, auth = _auth_preamble(base, timeout, password_command, username, password)
    lines.extend(auth_lines)

    error_states = ' '.join(shlex.quote(s) for s in sorted(_ERROR_STATES))
    lines.append(
        f'__torrents=$(curl -s -m {timeout} {auth} '
        f'{shlex.quote(base + "/api/v2/torrents/info")})'
    )
    lines.append(
        "__hashes=$(printf '%s' \"$__torrents\" | python3 -c "
        "\"import json,sys; states=set(sys.argv[1:]); "
        "data=json.load(sys.stdin); "
        "print('|'.join(t['hash'] for t in data if t.get('state') in states and t.get('hash')))\" "
        f"{error_states})"
    )
    lines.append('echo "HASHES:$__hashes"')
    lines.append('if [ -n "$__hashes" ]; then')
    lines.append(
        f'  curl -s -f -m {timeout} {auth} -H {shlex.quote("Referer: " + base)} '
        f'--data-urlencode "hashes=$__hashes" {shlex.quote(base + "/api/v2/torrents/recheck")}'
    )
    lines.append('fi')
    return '\n'.join(lines)


def _parse_response(stdout: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if _SEP not in stdout:
        raise ValueError(f"unexpected API response: {stdout[:200]!r}")
    transfer_raw, torrents_raw = stdout.split(_SEP, 1)

    transfer_raw, torrents_raw = transfer_raw.strip(), torrents_raw.strip()

    if transfer_raw.startswith('Forbidden'):
        raise ValueError(
            "API returned Forbidden (set username/password if auth is required)")

    try:
        transfer = json.loads(transfer_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"transfer info was not JSON ({e}): {transfer_raw[:200]!r}") from e
    try:
        torrents = json.loads(torrents_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"torrent list was not JSON ({e}): {torrents_raw[:200]!r}") from e

    if not isinstance(transfer, dict) or 'connection_status' not in transfer:
        raise ValueError(
            f"transfer info missing 'connection_status': {transfer_raw[:200]!r}")
    if not isinstance(torrents, list):
        raise ValueError(f"torrent list was not a list: {torrents_raw[:200]!r}")

    return transfer, torrents


def _format_rate(bytes_per_sec: float) -> str:
    value = float(bytes_per_sec)
    for unit in ('B/s', 'KiB/s', 'MiB/s', 'GiB/s'):
        if value < 1024 or unit == 'GiB/s':
            return f"{value:.1f} {unit}" if unit != 'B/s' else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} GiB/s"


_DEFAULT_LAYOUT = [
    ['host_card', 'connection_card', 'speed_card'],
    ['torrents_card', 'stalled_card', 'errored_card'],
    ['chart'],
    ['events'],
]


class Qbittorrent(Plugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.api_url = config.get('api_url', 'http://127.0.0.1:8080')
        self.username = config.get('username')
        self.password = config.get('password')
        self.password_command = config.get('password_command')
        self.stalled_warning = int(config.get('stalled_warning', 3))
        self.stalled_threshold = int(config.get('stalled_threshold', 10))
        self.error_threshold = int(config.get('error_threshold', 1))
        self.firewalled_warning = bool(config.get('firewalled_warning', True))
        self.min_downloading = int(config.get('min_downloading', 1))
        self.api_timeout = int(config.get('api_timeout', 10))

        from vigil.core.ui.spec import register_formatter, register_color_rule, register_item_formatter
        self._connection_format_name = f'qbittorrent_connection_{self.id}'
        register_formatter(self._connection_format_name)(
            lambda v: '--' if v is None else ('CONNECTED' if v >= 1.0 else 'DISCONNECTED'))
        self._connection_color_name = f'qbittorrent_connection_color_{self.id}'
        register_color_rule(self._connection_color_name)(
            lambda v: None if v is None else ('online' if v >= 1.0 else 'failed'))

        self._speed_format_name = f'qbittorrent_speed_{self.id}'
        register_item_formatter(self._speed_format_name)(self._speed_text)
        self._torrents_format_name = f'qbittorrent_torrents_{self.id}'
        register_item_formatter(self._torrents_format_name)(self._torrents_text)

        self._stalled_color_name = f'qbittorrent_stalled_color_{self.id}'
        register_color_rule(self._stalled_color_name)(self._stalled_color)
        self._errored_color_name = f'qbittorrent_errored_color_{self.id}'
        register_color_rule(self._errored_color_name)(
            lambda v: None if v is None else ('failed' if int(v) else 'online'))

    def commands(self) -> List[Command]:
        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.password_command,
            self.username, self.password,
        )
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            if _AUTH_FAILED in stderr:
                return CollectResult.failed(
                    "qBittorrent rejected the configured credentials "
                    "(check username / password_command)")
            return CollectResult.failed(f"Failed to query qBittorrent API: {stderr.strip()}")

        try:
            transfer, torrents = _parse_response(stdout)
        except ValueError as e:
            return CollectResult.failed(str(e))

        connection = str(transfer.get('connection_status', 'unknown'))
        dl_speed = float(transfer.get('dl_info_speed', 0) or 0)
        up_speed = float(transfer.get('up_info_speed', 0) or 0)

        stalled = [t for t in torrents if t.get('state') in _STALLED_STATES]
        errored = [t for t in torrents if t.get('state') in _ERROR_STATES]
        downloading = [t for t in torrents if t.get('state') in _ACTIVE_DL_STATES]

        metrics = {
            'dl_speed_bytes': dl_speed,
            'up_speed_bytes': up_speed,
            'torrents_total': float(len(torrents)),
            'torrents_stalled': float(len(stalled)),
            'torrents_errored': float(len(errored)),
            'torrents_downloading': float(len(downloading)),
            'dl_session_bytes': float(transfer.get('dl_info_data', 0) or 0),
            'up_session_bytes': float(transfer.get('up_info_data', 0) or 0),
            'connected': 1.0 if connection == 'connected' else 0.0,
        }

        problems = []
        level = 'online'

        def _escalate(new_level: str):
            nonlocal level
            order = ('online', 'warning', 'failed')
            if order.index(new_level) > order.index(level):
                level = new_level

        if connection == 'disconnected':
            problems.append("connection status is DISCONNECTED")
            _escalate('failed')
        elif connection == 'firewalled' and self.firewalled_warning:
            problems.append("connection is firewalled (no inbound peers)")
            _escalate('warning')

        if errored and len(errored) >= self.error_threshold:
            names = ', '.join(t.get('name', '?') for t in errored[:3])
            suffix = f" (+{len(errored) - 3} more)" if len(errored) > 3 else ""
            problems.append(f"{len(errored)} errored: {names}{suffix}")
            _escalate('failed')

        if len(downloading) >= self.min_downloading:
            if len(stalled) >= self.stalled_threshold:
                problems.append(
                    f"{len(stalled)} stalled (>= {self.stalled_threshold})")
                _escalate('failed')
            elif len(stalled) >= self.stalled_warning:
                problems.append(
                    f"{len(stalled)} stalled (>= {self.stalled_warning})")
                _escalate('warning')

        parts = [
            f"{connection}",
            f"↓ {_format_rate(dl_speed)}",
            f"↑ {_format_rate(up_speed)}",
            f"{len(torrents)} torrents",
            f"{len(downloading)} downloading",
        ]
        if problems:
            parts.append("| " + "; ".join(problems))

        log_level = "ERROR" if level == 'failed' else "WARNING" if level == 'warning' else "INFO"
        return CollectResult(metrics=metrics, logs=[(' | '.join(parts), log_level)], status=level)

    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {'name': 'Resume All', 'action_id': 'resume_all',
             'variant': 'primary', 'icon': 'play_arrow'},
            {'name': 'Recheck Errored', 'action_id': 'recheck_errored',
             'variant': 'secondary', 'icon': 'fact_check'},
            {'name': 'Pause All', 'action_id': 'pause_all',
             'variant': 'danger', 'icon': 'pause'},
        ]

    def plan_action(self, action_id: str, **kwargs) -> Optional[Union[ActionPlan, CollectResult]]:
        if action_id == 'resume_all':
            script = _build_fallback_action_script(
                self.api_url, self.api_timeout, self.password_command,
                self.username, self.password,
                '/api/v2/torrents/start', '/api/v2/torrents/resume', {'hashes': 'all'},
            )
            return ActionPlan(script)

        if action_id == 'pause_all':
            script = _build_fallback_action_script(
                self.api_url, self.api_timeout, self.password_command,
                self.username, self.password,
                '/api/v2/torrents/stop', '/api/v2/torrents/pause', {'hashes': 'all'},
            )
            return ActionPlan(script)

        if action_id == 'recheck_errored':
            script = _build_recheck_script(
                self.api_url, self.api_timeout, self.password_command,
                self.username, self.password,
            )
            return ActionPlan(script)

        return None

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        if action_id == 'resume_all':
            if result.exit_code != 0:
                if _AUTH_FAILED in (result.stderr or ''):
                    return CollectResult.failed(
                        "resume_all rejected: qBittorrent refused the configured credentials")
                return CollectResult.failed(f"resume_all failed: {(result.stderr or '').strip()}")
            return CollectResult(logs=[("Resumed all torrents", "INFO")], success=True)

        if action_id == 'pause_all':
            if result.exit_code != 0:
                if _AUTH_FAILED in (result.stderr or ''):
                    return CollectResult.failed(
                        "pause_all rejected: qBittorrent refused the configured credentials")
                return CollectResult.failed(f"pause_all failed: {(result.stderr or '').strip()}")
            return CollectResult(logs=[("Paused all torrents", "WARNING")], success=True)

        if action_id == 'recheck_errored':
            if result.exit_code != 0:
                if _AUTH_FAILED in (result.stderr or ''):
                    return CollectResult.failed(
                        "recheck_errored rejected: qBittorrent refused the configured credentials")
                return CollectResult.failed(f"Could not list/recheck torrents: {(result.stderr or '').strip()}")
            hashes_line = next(
                (line for line in result.stdout.splitlines() if line.startswith('HASHES:')), 'HASHES:')
            hashes = [h for h in hashes_line[len('HASHES:'):].split('|') if h]
            if not hashes:
                return CollectResult(logs=[("No errored torrents to recheck", "INFO")], success=True)
            return CollectResult(
                logs=[(f"Rechecking {len(hashes)} errored torrent(s)", "INFO")], success=True)

        return result.exit_code == 0

    @staticmethod
    def _speed_text(values: Dict[str, Any]) -> str:
        dl, up = values.get('dl_speed_bytes'), values.get('up_speed_bytes')
        if dl is None or up is None:
            return '--'
        return f'↓ {_format_rate(dl)}  ↑ {_format_rate(up)}'

    @staticmethod
    def _torrents_text(values: Dict[str, Any]) -> str:
        total = values.get('torrents_total')
        if total is None:
            return '--'
        text = f'{int(total)}'
        downloading = values.get('torrents_downloading')
        if downloading is not None:
            text += f' ({int(downloading)} active)'
        return text

    def _stalled_color(self, v: Optional[float]) -> Optional[str]:
        if v is None:
            return None
        count = int(v)
        if count >= self.stalled_threshold:
            return 'failed'
        if count >= self.stalled_warning:
            return 'warning'
        return 'online'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'connection_card': {'metric': 'connected', 'title': 'CONNECTION',
                                    'format': self._connection_format_name, 'color': self._connection_color_name},
                'speed_card': {'title': 'TRANSFER', 'metrics': ['dl_speed_bytes', 'up_speed_bytes'],
                              'format_fn': self._speed_format_name},
                'torrents_card': {'title': 'TORRENTS', 'metrics': ['torrents_total', 'torrents_downloading'],
                                  'format_fn': self._torrents_format_name},
                'stalled_card': {'metric': 'torrents_stalled', 'title': 'STALLED', 'format': 'int',
                                 'color': self._stalled_color_name},
                'errored_card': {'metric': 'torrents_errored', 'title': 'ERRORED', 'format': 'int',
                                 'color': self._errored_color_name},
            },
            'chart': {'metric': 'dl_speed_bytes', 'title': 'DOWNLOAD SPEED (B/s)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)
