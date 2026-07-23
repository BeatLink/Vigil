"""
qBittorrent transfer health via the WebUI API (v2).

Complements a `systemd_service` monitor on qbittorrent rather than replacing it.
That one answers "is the process alive"; this one answers "is it still doing its
job". The case that motivates it: the daemon keeps running while transfers have
silently stopped — a VPN tunnel dropped, the listening port stopped being
forwarded, or the disk filled — and every liveness check stays green throughout.
The service is up, the WebUI answers, the port is open, and the only visible
symptom is a queue of torrents that never finish.

Three signals carry that:

  connection status  qBittorrent's own view of its reachability. "disconnected"
                     means no peer connectivity at all; "firewalled" means it can
                     reach peers but none can reach it, which starves seeding and
                     slows swarms without stopping them outright.
  stalled downloads  Torrents that are incomplete and moving no bytes. A handful
                     is normal for a dead swarm; the whole queue stalling at once
                     is the tunnel, not the torrents.
  errored torrents   Torrents qBittorrent has itself marked errored or missing
                     files — usually the storage path disappeared.

All three are checked because they fail on different clocks: a firewalled client
still completes downloads slowly, and a full disk errors torrents while the
connection stays green. None alone is sufficient.

The API is read over SSH with curl rather than from Vigil's own process, matching
how `pihole` and `ports` probe. The WebUI commonly binds to loopback or sits
behind an authenticating reverse proxy, so it is not reliably reachable across
the network; reading it from the monitored host sidesteps both.

qBittorrent authenticates with a cookie-based session: POST credentials to
`/api/v2/auth/login`, then send the returned SID cookie on subsequent calls.
Where the WebUI is configured to bypass authentication for localhost (the common
setup for a loopback bind), no credentials are needed; set `username` and
`password_command` if the instance requires them. A rejected login is reported
as such rather than surfacing later as an opaque "Forbidden", because curl exits
successfully on an HTTP 403 and qBittorrent signals bad credentials in the
response body.

Control actions:

  Resume All        Starts every torrent. The remediation for a queue left
                    paused by a stall or an unclean restart.
  Recheck Errored   Re-verifies only the torrents in an error state, the usual
                    fix once a missing storage path is back. Scoped to those
                    torrents because a recheck re-reads every piece from disk.
  Pause All         Stops every torrent — the one action offered for taking load
                    off the array deliberately.

Destructive operations (deleting torrents or data) are deliberately not exposed:
the dashboard fires actions immediately with no confirmation step, so a mis-click
must not be able to cost data.

Config options:
  api_url             Base URL of the WebUI, as seen from the monitored host
                      (default: http://127.0.0.1:8080)
  username            WebUI username, if the instance requires authentication.
                      Credentials are only used when this is set alongside one
                      of the two password options below.
  password            WebUI password. Prefer password_command over inlining a
                      secret here — this value is readable in the config file.
  password_command    Command run on the monitored host whose stdout is the
                      password (e.g. "cat /run/secrets/qbittorrent_api"). Takes
                      precedence over `password`.
  stalled_warning     Stalled-download count at or above which status is warning
                      (default: 3)
  stalled_threshold   Stalled-download count at or above which status is failed
                      (default: 10)
  error_threshold     Errored-torrent count at or above which status is failed
                      (default: 1)
  firewalled_warning  Whether a "firewalled" connection status is a warning
                      (default: true). Set false where inbound connections are
                      not expected and the state is permanent.
  min_downloading     Active downloads needed before the stall counts are judged
                      at all (default: 1). Below this there is nothing to stall.
"""
import json
import shlex
from typing import Any, Dict, List, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

# Marks the end of the transfer payload so both API responses can be fetched in
# one SSH round trip and split apart again. A newline-free sentinel that cannot
# occur in JSON.
_SEP = "@@VIGIL_SPLIT@@"

# Emitted on stderr by the remote script when login is rejected, so a wrong
# password is reported as such instead of as a generic command failure.
_AUTH_FAILED = "VIGIL_AUTH_FAILED"

# Torrent states qBittorrent reports for an incomplete transfer that is making
# no progress. "stalledDL" is the ordinary one; the queued/stalled metadata
# states cover a torrent that cannot even fetch its own info dictionary, which
# is the shape a dead tunnel takes for a freshly added magnet link.
_STALLED_STATES = {'stalledDL', 'metaDL', 'stalledUP'}

# States qBittorrent uses to mark a torrent it has given up on. Distinct from
# stalling: these do not recover on their own.
_ERROR_STATES = {'error', 'missingFiles'}

# Incomplete-but-progressing states, counted to tell "nothing is downloading"
# apart from "downloads are stuck".
_ACTIVE_DL_STATES = {'downloading', 'metaDL', 'stalledDL', 'queuedDL',
                     'forcedDL', 'checkingDL', 'allocating', 'downloadingMetadata'}


def _auth_preamble(base: str, timeout: int, password_command: Optional[str],
                   username: Optional[str], password: Optional[str]) -> Tuple[List[str], str]:
    """
    Build the shell lines that establish a session, and the curl flags that use it.

    Returns (lines, auth_flags). When no credentials are configured the lines are
    empty and the flags blank, which is the correct shape for an instance that
    bypasses authentication for localhost.

    The password is resolved on the monitored host when `password_command` is
    used, so the secret never passes through Vigil's process or its config file.
    """
    if not (username and (password_command or password)):
        return [], ''

    lines = []
    if password_command:
        lines.append(f"__pw=$({password_command})")
    else:
        lines.append(f"__pw={shlex.quote(password)}")

    # qBittorrent hands the session back as a Set-Cookie header rather than in a
    # body, so a cookie jar is what carries auth forward. A private temp jar is
    # used rather than the invoking user's own, and removed on any exit path —
    # it holds a live session credential.
    lines.append('__jar=$(mktemp)')
    lines.append("""trap 'rm -f "$__jar"' EXIT INT TERM""")
    # `set -e` does not fire on a failed login, because curl exits 0 for an HTTP
    # 403 and qBittorrent signals bad credentials in the body ("Fails."), not the
    # status line. Checking it here turns a wrong password into one clear error
    # instead of two confusing "Forbidden" responses further down.
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
    """
    Build a shell script that fetches the transfer info and torrent list.

    Authenticates first when credentials are supplied, reusing the returned
    session cookie for both calls.
    """
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
    """
    Build a shell script that POSTs to a control endpoint.

    Fails loudly rather than silently: qBittorrent answers a rejected action with
    a non-2xx status and an empty body, so `--fail` is what turns that into a
    non-zero exit the controller can report.
    """
    base = api_url.rstrip('/')
    lines = ["set -e"]

    auth_lines, auth = _auth_preamble(base, timeout, password_command, username, password)
    lines.extend(auth_lines)

    # The Referer header is not optional for state-changing calls: qBittorrent
    # rejects them with 403 as CSRF protection when it is absent.
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
    """
    Split and parse the two JSON payloads.

    Raises ValueError with a readable message on anything unparseable, so the
    caller can log one cause rather than a bare KeyError.
    """
    if _SEP not in stdout:
        raise ValueError(f"unexpected API response: {stdout[:200]!r}")
    transfer_raw, torrents_raw = stdout.split(_SEP, 1)

    transfer_raw, torrents_raw = transfer_raw.strip(), torrents_raw.strip()

    # An instance that requires authentication answers with the bare string
    # "Forbidden" and a 403 rather than JSON; say so plainly instead of
    # reporting a decode error the operator has to work backwards from.
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
    """Format a transfer rate as a compact human-readable string."""
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
    """Monitors qBittorrent transfer health via the WebUI API."""

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
        # Seconds allowed for the remote curl calls. Distinct from self.timeout,
        # which bounds the SSH command as a whole and must stay the larger.
        self.api_timeout = int(config.get('api_timeout', 10))

    async def on_collect(self):
        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.password_command,
            self.username, self.password,
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            if _AUTH_FAILED in stderr:
                # Distinct from an unreachable API: the credentials are wrong or
                # the account is banned after repeated failures, and no amount of
                # retrying fixes either.
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

        # Bucket the queue by state in one pass. Torrents with no 'state' key
        # are counted as neither stalled nor errored rather than crashing the
        # cycle — a partial payload should not take the monitor down.
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

        # --- status ---------------------------------------------------------
        # Each condition is judged independently and the worst one wins, so a
        # healthy transfer rate cannot mask an errored queue (or vice versa).
        problems = []
        level = 'online'

        def _escalate(new_level: str):
            nonlocal level
            order = ('online', 'warning', 'failed')
            if order.index(new_level) > order.index(level):
                level = new_level

        if connection == 'disconnected':
            # No peer connectivity at all. On a VPN-bound client this is what a
            # dropped tunnel looks like from the inside.
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

        # Below min_downloading there is nothing in flight for stalling to be
        # evidence about — an idle client with a finished queue is healthy.
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
        """
        Expose the remediations that fix the faults this monitor detects.

        Deliberately limited to queue-wide, reversible operations. The dashboard
        fires actions immediately with no confirmation step, so anything
        destructive (deleting torrents, wiping data) is not offered here at all —
        a mis-click must never cost data. Resume/pause and a recheck cover the
        realistic recovery paths: resuming what a stall or a restart left paused,
        and rechecking torrents that errored because their files went missing.
        """
        return [
            {'name': 'Resume All', 'action_id': 'resume_all',
             'variant': 'primary', 'icon': 'play_arrow'},
            {'name': 'Recheck Errored', 'action_id': 'recheck_errored',
             'variant': 'secondary', 'icon': 'fact_check'},
            {'name': 'Pause All', 'action_id': 'pause_all',
             'variant': 'danger', 'icon': 'pause'},
        ]

    async def _post(self, endpoint: str, params: Optional[Dict[str, str]] = None) -> bool:
        """Run a control endpoint on the monitored host, logging any failure."""
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
        # `hashes=all` is qBittorrent's documented queue-wide selector, so these
        # need no torrent list to act on and cannot act on a stale one.
        if action_id == 'resume_all':
            # The endpoint was renamed in qBittorrent 5.0 (resume -> start) and
            # the old path 404s there, so the modern name is tried first and the
            # legacy one only if it is genuinely absent. Falling back on any
            # failure would retry real errors twice and mask the true cause.
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
            # Scoped to the torrents actually in an error state rather than the
            # whole queue: a recheck re-reads every piece from disk, so running
            # it across a large healthy library is hours of pointless I/O on the
            # array this monitor is meant to protect.
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
        """
        Return the hashes of currently-errored torrents, or None if unreadable.

        Read fresh rather than cached from the last poll: the queue may have
        changed in the minutes since, and a recheck aimed at a stale hash either
        does nothing or hits a torrent that has since recovered.
        """
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
    """Dashboard rendering for the qbittorrent monitor."""

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart, on_data_event
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        stalled_warning = int(self.config.get('stalled_warning', 3))
        stalled_threshold = int(self.config.get('stalled_threshold', 10))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('connection_card'):
            connection_label = info_card('CONNECTION', '--')
        with layout.cell('speed_card'):
            speed_label = info_card('TRANSFER', '--')
        with layout.cell('torrents_card'):
            torrents_label = info_card('TORRENTS', '--')
        with layout.cell('stalled_card'):
            stalled_label = info_card('STALLED', '--')
        with layout.cell('errored_card'):
            errored_label = info_card('ERRORED', '--')
        with layout.cell('chart'):
            history_chart('DOWNLOAD SPEED (B/s)', self.id, 'dl_speed_bytes')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            connected   = self.latest_metric('connected')
            dl          = self.latest_metric('dl_speed_bytes')
            up          = self.latest_metric('up_speed_bytes')
            total       = self.latest_metric('torrents_total')
            downloading = self.latest_metric('torrents_downloading')
            stalled     = self.latest_metric('torrents_stalled')
            errored     = self.latest_metric('torrents_errored')

            if connected:
                on = connected.value >= 1.0
                connection_label.text = 'CONNECTED' if on else 'DISCONNECTED'
                connection_label.style(
                    f'color: {STATUS_COLORS["online" if on else "failed"]}')
            if dl and up:
                speed_label.text = (
                    f'↓ {_format_rate(dl.value)}  ↑ {_format_rate(up.value)}')
            if total:
                torrents_label.text = f'{int(total.value)}'
                if downloading:
                    torrents_label.text += f' ({int(downloading.value)} active)'
            if stalled:
                count = int(stalled.value)
                stalled_label.text = str(count)
                if count >= stalled_threshold:
                    colour = STATUS_COLORS['failed']
                elif count >= stalled_warning:
                    colour = STATUS_COLORS['warning']
                else:
                    colour = STATUS_COLORS['online']
                stalled_label.style(f'color: {colour}')
            if errored:
                count = int(errored.value)
                errored_label.text = str(count)
                errored_label.style(
                    f'color: {STATUS_COLORS["failed" if count else "online"]}')

        on_data_event('metric', connection_label, update_cards)
