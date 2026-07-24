import json
import shlex
from typing import Any, Dict, List, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

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


def _build_action_script(api_url: str, timeout: int, password_command: Optional[str],
                         username: Optional[str], password: Optional[str],
                         endpoint: str, params: Optional[Dict[str, str]] = None) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    auth_lines, auth = _auth_preamble(base, timeout, password_command, username, password)
    lines.extend(auth_lines)

    parts = [
        f'curl -s -f -m {timeout} {auth}',
        f'-H {shlex.quote("Referer: " + base)}',
    ]
    for key, value in (params or {}).items():
        parts.append(f'--data-urlencode {shlex.quote(f"{key}={value}")}')
    parts.append(shlex.quote(base + endpoint))
    lines.append(' '.join(parts))
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


class QbittorrentCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
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

    async def on_collect(self):
        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.password_command,
            self.username, self.password,
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            if _AUTH_FAILED in stderr:
                self.db_logger.write(
                    "qBittorrent rejected the configured credentials "
                    "(check username / password_command)", level="ERROR")
            else:
                self.db_logger.write(
                    f"Failed to query qBittorrent API: {stderr.strip()}", level="ERROR")
            self.set_status('failed')
            return

        try:
            transfer, torrents = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.set_status('failed')
            return

        connection = str(transfer.get('connection_status', 'unknown'))
        dl_speed = float(transfer.get('dl_info_speed', 0) or 0)
        up_speed = float(transfer.get('up_info_speed', 0) or 0)

        stalled = [t for t in torrents if t.get('state') in _STALLED_STATES]
        errored = [t for t in torrents if t.get('state') in _ERROR_STATES]
        downloading = [t for t in torrents if t.get('state') in _ACTIVE_DL_STATES]

        self.db_metrics.metric('dl_speed_bytes', dl_speed)
        self.db_metrics.metric('up_speed_bytes', up_speed)
        self.db_metrics.metric('torrents_total', float(len(torrents)))
        self.db_metrics.metric('torrents_stalled', float(len(stalled)))
        self.db_metrics.metric('torrents_errored', float(len(errored)))
        self.db_metrics.metric('torrents_downloading', float(len(downloading)))
        self.db_metrics.metric('dl_session_bytes', float(transfer.get('dl_info_data', 0) or 0))
        self.db_metrics.metric('up_session_bytes', float(transfer.get('up_info_data', 0) or 0))
        self.db_metrics.metric('connected', 1.0 if connection == 'connected' else 0.0)

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
        self.db_logger.write(' | '.join(parts), level=log_level)
        self.set_status(level)

    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {'name': 'Resume All', 'action_id': 'resume_all',
             'variant': 'primary', 'icon': 'play_arrow'},
            {'name': 'Recheck Errored', 'action_id': 'recheck_errored',
             'variant': 'secondary', 'icon': 'fact_check'},
            {'name': 'Pause All', 'action_id': 'pause_all',
             'variant': 'danger', 'icon': 'pause'},
        ]

    async def _post(self, endpoint: str, params: Optional[Dict[str, str]] = None) -> bool:
        script = _build_action_script(
            self.api_url, self.api_timeout, self.password_command,
            self.username, self.password, endpoint, params,
        )
        status, _, stderr = await self.ssh_controller.execute_action(script)
        if status != 0:
            if _AUTH_FAILED in (stderr or ''):
                self.db_logger.write(
                    f"{endpoint} rejected: qBittorrent refused the configured "
                    "credentials", level="ERROR")
            else:
                self.db_logger.write(
                    f"{endpoint} failed: {(stderr or '').strip()}", level="ERROR")
        return status == 0

    async def on_action(self, action_id: str, **kwargs) -> bool:
        if action_id == 'resume_all':
            if await self._post('/api/v2/torrents/start', {'hashes': 'all'}):
                self.db_logger.write("Resumed all torrents", level="INFO")
                return True
            if await self._post('/api/v2/torrents/resume', {'hashes': 'all'}):
                self.db_logger.write("Resumed all torrents", level="INFO")
                return True
            return False

        if action_id == 'pause_all':
            if await self._post('/api/v2/torrents/stop', {'hashes': 'all'}):
                self.db_logger.write("Paused all torrents", level="WARNING")
                return True
            if await self._post('/api/v2/torrents/pause', {'hashes': 'all'}):
                self.db_logger.write("Paused all torrents", level="WARNING")
                return True
            return False

        if action_id == 'recheck_errored':
            hashes = await self._errored_hashes()
            if hashes is None:
                return False
            if not hashes:
                self.db_logger.write("No errored torrents to recheck", level="INFO")
                return True
            ok = await self._post('/api/v2/torrents/recheck', {'hashes': '|'.join(hashes)})
            if ok:
                self.db_logger.write(
                    f"Rechecking {len(hashes)} errored torrent(s)", level="INFO")
            return ok

        return False

    async def _errored_hashes(self) -> Optional[List[str]]:
        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.password_command,
            self.username, self.password,
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            self.db_logger.write(
                f"Could not list torrents to recheck: {(stderr or '').strip()}",
                level="ERROR")
            return None
        try:
            _, torrents = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(f"Could not list torrents to recheck: {e}", level="ERROR")
            return None
        return [t['hash'] for t in torrents
                if t.get('state') in _ERROR_STATES and t.get('hash')]


class QbittorrentUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.spec import FORMATTERS
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        stalled_warning = int(self.config.get('stalled_warning', 3))
        stalled_threshold = int(self.config.get('stalled_threshold', 10))

        page = self.page(metric_names=[
            'connected', 'dl_speed_bytes', 'up_speed_bytes', 'torrents_total',
            'torrents_downloading', 'torrents_stalled', 'torrents_errored',
        ])

        def _connection_text(v):
            if v is None:
                return '--'
            return 'CONNECTED' if v >= 1.0 else 'DISCONNECTED'

        _int_or_dash = FORMATTERS['int']

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('connection_card'):
            connection_label = info_card('CONNECTION', '--').bind_text_from(
                page.model, ('metrics', 'connected'), backward=_connection_text)
        with layout.cell('speed_card'):
            speed_label = info_card('TRANSFER', '--')
        with layout.cell('torrents_card'):
            torrents_label = info_card('TORRENTS', '--')
        with layout.cell('stalled_card'):
            stalled_label = info_card('STALLED', '--').bind_text_from(
                page.model, ('metrics', 'torrents_stalled'), backward=_int_or_dash)
        with layout.cell('errored_card'):
            errored_label = info_card('ERRORED', '--').bind_text_from(
                page.model, ('metrics', 'torrents_errored'), backward=_int_or_dash)
        with layout.cell('chart'):
            history_chart(page, 'DOWNLOAD SPEED (B/s)', self.id, 'dl_speed_bytes')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_cards():
            metrics = page.model.metrics
            connected   = metrics.get('connected')
            dl          = metrics.get('dl_speed_bytes')
            up          = metrics.get('up_speed_bytes')
            total       = metrics.get('torrents_total')
            downloading = metrics.get('torrents_downloading')
            stalled     = metrics.get('torrents_stalled')
            errored     = metrics.get('torrents_errored')

            if connected is not None:
                on = connected >= 1.0
                connection_label.style(
                    f'color: {STATUS_COLORS["online" if on else "failed"]}')
            if dl is not None and up is not None:
                speed_label.text = (
                    f'↓ {_format_rate(dl)}  ↑ {_format_rate(up)}')
            if total is not None:
                text = f'{int(total)}'
                if downloading is not None:
                    text += f' ({int(downloading)} active)'
                torrents_label.text = text
            if stalled is not None:
                count = int(stalled)
                if count >= stalled_threshold:
                    colour = STATUS_COLORS['failed']
                elif count >= stalled_warning:
                    colour = STATUS_COLORS['warning']
                else:
                    colour = STATUS_COLORS['online']
                stalled_label.style(f'color: {colour}')
            if errored is not None:
                count = int(errored)
                errored_label.style(
                    f'color: {STATUS_COLORS["failed" if count else "online"]}')

        page.on_refresh(update_cards)
        page.start()
