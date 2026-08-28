"""qBittorrent transfer health via its WebUI API, fetched by one curl script
over SSH — sampled locally by the agent on agent-backed hosts — that logs in,
reads transfer info plus the torrent list, and logs the session out on exit.
Config: api_url, username, password / password_command, stalled_warning,
stalled_threshold, error_threshold, firewalled_warning, min_downloading,
api_timeout. A DISCONNECTED connection status, errored torrents at
error_threshold, or stalls at stalled_threshold are failed; a firewalled
connection or stalls at stalled_warning are warning (stall counts only apply
while at least min_downloading torrents are downloading)."""

import json
import shlex
from typing import Any, Dict, List, Optional, Tuple, Union

from vigil.plugins.base.plugin_base import Plugin
from vigil.plugins.base.plugin_helpers import (
    SCRIPT_SEP, StatusAccumulator, dq, password_line,
)
from vigil.core.connectors.types import ActionPlan, CmdResult, Command, CollectResult

_AUTH_FAILED = "VIGIL_AUTH_FAILED"

_STALLED_STATES = {'stalledDL', 'metaDL', 'stalledUP'}

_ERROR_STATES = {'error', 'missingFiles'}

_ACTIVE_DL_STATES = {'downloading', 'metaDL', 'stalledDL', 'queuedDL',
                     'forcedDL', 'checkingDL', 'allocating', 'downloadingMetadata'}


def _auth_preamble(base: str, timeout: int, password_command: Optional[str],
                   username: Optional[str], password: Optional[str]) -> Tuple[List[str], str]:
    if not (username and (password_command or password)):
        return [], ''

    lines = [password_line(password_command, password)]
    lines.append('__jar=$(mktemp)')
    # Log the WebUI session out on any exit, then drop the cookie jar, so a
    # poll never leaves a live session behind.
    lines.append(
        "trap 'curl -s -m %d -b \"$__jar\" -H %s --data \"\" %s "
        ">/dev/null 2>&1; rm -f \"$__jar\"' EXIT INT TERM"
        % (timeout, dq("Referer: " + base), dq(base + "/api/v2/auth/logout"))
    )
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
    lines.append(f'echo "{SCRIPT_SEP}"')
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
    if SCRIPT_SEP not in stdout:
        raise ValueError(f"unexpected API response: {stdout[:200]!r}")
    transfer_raw, torrents_raw = stdout.split(SCRIPT_SEP, 1)

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


def _classify_torrents(torrents: List[Dict[str, Any]]):
    """Split the torrent list into stalled, errored, and actively-downloading subsets."""
    stalled = [t for t in torrents if t.get('state') in _STALLED_STATES]
    errored = [t for t in torrents if t.get('state') in _ERROR_STATES]
    downloading = [t for t in torrents if t.get('state') in _ACTIVE_DL_STATES]
    return stalled, errored, downloading


def _accumulate_transfer_problems(connection: str, stalled: List[Dict[str, Any]],
                                  errored: List[Dict[str, Any]],
                                  downloading: List[Dict[str, Any]],
                                  firewalled_warning: bool, error_threshold: int,
                                  stalled_warning: int, stalled_threshold: int,
                                  min_downloading: int) -> StatusAccumulator:
    """Judge the connection state and stalled/errored torrent counts against the thresholds."""
    acc = StatusAccumulator()

    if connection == 'disconnected':
        acc.escalate('failed', "connection status is DISCONNECTED")
    elif connection == 'firewalled' and firewalled_warning:
        acc.escalate('warning', "connection is firewalled (no inbound peers)")

    if errored and len(errored) >= error_threshold:
        names = ', '.join(t.get('name', '?') for t in errored[:3])
        suffix = f" (+{len(errored) - 3} more)" if len(errored) > 3 else ""
        acc.escalate('failed', f"{len(errored)} errored: {names}{suffix}")

    if len(downloading) >= min_downloading:
        if len(stalled) >= stalled_threshold:
            acc.escalate('failed', f"{len(stalled)} stalled (>= {stalled_threshold})")
        elif len(stalled) >= stalled_warning:
            acc.escalate('warning', f"{len(stalled)} stalled (>= {stalled_warning})")

    return acc


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
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
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

        self._connection_format = (
            lambda v: '--' if v is None else ('CONNECTED' if v >= 1.0 else 'DISCONNECTED'))
        self._connection_color = (
            lambda v: None if v is None else ('online' if v >= 1.0 else 'failed'))
        self._errored_color = (
            lambda v: None if v is None else ('failed' if int(v) else 'online'))

    SAMPLED = True

    def commands(self) -> List[Command]:
        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.password_command,
            self.username, self.password,
        )
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        """Turns the transfer-info+torrent-list curl output into a CollectResult
        with speed/count metrics, one summary log line, and a status where a
        DISCONNECTED link, errored torrents, or heavy stalling is failed and a
        firewalled link or moderate stalling is warning."""
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

        stalled, errored, downloading = _classify_torrents(torrents)

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

        acc = _accumulate_transfer_problems(
            connection, stalled, errored, downloading,
            self.firewalled_warning, self.error_threshold,
            self.stalled_warning, self.stalled_threshold, self.min_downloading)

        parts = [
            f"{connection}",
            f"↓ {_format_rate(dl_speed)}",
            f"↑ {_format_rate(up_speed)}",
            f"{len(torrents)} torrents",
            f"{len(downloading)} downloading",
        ]
        if acc.problems:
            parts.append("| " + "; ".join(acc.problems))

        return CollectResult(metrics=metrics, logs=[(' | '.join(parts), acc.log_level)], status=acc.status)

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
                                    'format': self._connection_format, 'color': self._connection_color},
                'speed_card': {'title': 'TRANSFER', 'metrics': ['dl_speed_bytes', 'up_speed_bytes'],
                              'format_fn': self._speed_text},
                'torrents_card': {'title': 'TORRENTS', 'metrics': ['torrents_total', 'torrents_downloading'],
                                  'format_fn': self._torrents_text},
                'stalled_card': {'metric': 'torrents_stalled', 'title': 'STALLED', 'format': 'int',
                                 'color': self._stalled_color},
                'errored_card': {'metric': 'torrents_errored', 'title': 'ERRORED', 'format': 'int',
                                 'color': self._errored_color},
            },
            'chart': {'metric': 'dl_speed_bytes', 'title': 'DOWNLOAD SPEED (B/s)'},
            'events': True,
        }

