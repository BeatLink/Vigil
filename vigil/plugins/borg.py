import json
import shlex
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.time_utils import parse_duration, format_duration, format_age
from vigil.core.ui.components import info_card


_DEFAULT_LAYOUT = [
    ['host_card', 'repo_card', 'maxage_card', 'state_card'],
    ['history'],
    ['logs'],
]


def _parse_archive_time(value: str) -> int:
    """
    Parse a Borg archive timestamp into a unix epoch (int seconds).

    Borg emits ISO-8601 times such as "2024-05-01T03:14:07.000000" (local time,
    no offset) and, in newer versions, offset-aware forms like
    "2024-05-01T03:14:07+00:00". Returns 0 if the value can't be parsed.
    """
    if not value:
        return 0
    text = value.strip()
    # Python's fromisoformat rejects a trailing 'Z' before 3.11; normalise it.
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0
    return int(dt.timestamp())


class BorgPlugin(BasePlugin):
    """
    Monitors a BorgBackup repository directly over SSH.

    Borgmatic and Vorta are both just schedulers wrapping `borg`; this plugin
    checks the repository itself, which is the authoritative source of truth —
    it verifies an archive actually exists and is recent, rather than trusting a
    scheduler's local bookkeeping.

    Each cycle it runs `borg list --last 1 --json` on the repo's SSH host and
    reads the newest archive's timestamp. Reports online if the newest archive
    is within `max_age`, failed if the repo has no archives, the newest is stale,
    or borg errors (repo locked, wrong passphrase, unreachable, ...).

    The `borg` binary must be available on the SSH host. For an ssh:// repo URL,
    borg opens its own connection from that host to the repo server, so the host
    running borg needs SSH access to the repo server (Borg's normal model). For a
    local-path repo, borg reads it directly on the host.

    Config:
      repo               Repository URL or local path (e.g.
                         "ssh://borg@host/srv/repo" or "/mnt/backups/repo").
                         Required.
      max_age            Maximum acceptable age of the newest archive
                         (e.g. "1d", "36h"). Defaults to "1d".
      passphrase         Repo passphrase for encrypted repos. Exported as
                         BORG_PASSPHRASE for the borg call. Optional.
      passphrase_command A shell command whose stdout is the passphrase (exported
                         as BORG_PASSCOMMAND, e.g. "cat /etc/vigil/repo.pass").
                         Mutually exclusive with `passphrase`. Optional.
      borg_bin           Path to the borg executable. Defaults to "borg".
      lock_wait          Seconds borg waits for a repo lock before giving up, so a
                         concurrent backup does not hang the poll. Defaults to 5.
      ssh_config.host    Host to run borg on (via BasePlugin's SSH config).
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.repo = config.get('repo')
        self.max_age = parse_duration(config.get('max_age', '1d'))
        self.passphrase = config.get('passphrase')
        self.passphrase_command = config.get('passphrase_command')
        self.borg_bin = config.get('borg_bin', 'borg')
        self.lock_wait = config.get('lock_wait', 5)

    # -------------------------------------------------------------------------
    # Collection
    # -------------------------------------------------------------------------

    def _list_command(self) -> str:
        """
        Build the shell command run on the SSH host: query the newest archive as
        JSON. Any passphrase is passed as an environment prefix so it never
        appears in argv (which is world-visible via `ps`).

        All arguments are shlex-quoted so repo paths/URLs with spaces are safe.
        """
        env = []
        if self.passphrase_command is not None:
            env.append("BORG_PASSCOMMAND=" + shlex.quote(self.passphrase_command))
        elif self.passphrase is not None:
            env.append("BORG_PASSPHRASE=" + shlex.quote(self.passphrase))
        # Never prompt interactively — fail fast instead of hanging the poll.
        env.append("BORG_RELOCATED_REPO_ACCESS_IS_OK=no")

        args = [
            self.borg_bin, "list",
            "--last", "1",
            "--json",
            "--lock-wait", str(self.lock_wait),
            self.repo,
        ]
        return " ".join(env + [shlex.quote(a) for a in args])

    async def on_collect(self):
        if not self.repo:
            self.db_logger.write("No 'repo' configured for borg monitor", level="ERROR")
            self.set_status('failed')
            return

        ret, stdout, stderr = await self.ssh_collector.fetch_output(self._list_command())

        if ret != 0:
            self.db_logger.write(
                f"borg list failed: {(stderr or stdout).strip()}", level="ERROR"
            )
            self.set_status('failed')
            return

        latest_epoch, archive_count = self._newest_archive(stdout)

        if latest_epoch is None:
            self.db_logger.write(
                "Could not parse borg output — no archive timestamps found",
                level="ERROR"
            )
            self.set_status('failed')
            return

        self.db_metrics.metric('archive_count', float(archive_count))
        self.db_metrics.metric('last_backup_epoch', float(latest_epoch))

        if archive_count == 0 or latest_epoch == 0:
            self.db_logger.write("No archives in repository", level="WARNING")
            self.set_status('failed')
        else:
            age = int(time.time()) - latest_epoch
            if age > self.max_age:
                self.db_logger.write(
                    f"Last archive was {format_age(age)}, exceeds max_age of "
                    f"{format_duration(self.max_age)}",
                    level="WARNING"
                )
                self.set_status('failed')
            else:
                self.db_logger.write(
                    f"Last archive {format_age(age)}", level="INFO"
                )
                self.set_status('online')

    def _newest_archive(self, stdout: str) -> (Optional[int], int):
        """
        Parse `borg list --json` output into (newest_epoch, archive_count).

        Returns (None, 0) when the output is not valid JSON — the caller treats
        this as a hard failure. A valid but empty repository yields (0, 0).
        `borg list --last 1` returns at most one archive in the "archives" array.
        """
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return None, 0

        if not isinstance(data, dict):
            return None, 0

        archives = data.get('archives') or []
        if not isinstance(archives, list):
            return None, 0

        newest = 0
        for archive in archives:
            if not isinstance(archive, dict):
                continue
            # Borg uses "start" for archive creation time; "time" as a fallback.
            epoch = _parse_archive_time(archive.get('start') or archive.get('time', ''))
            if epoch > newest:
                newest = epoch

        return newest, len(archives)

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.ui.theme import STATUS_COLORS
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT)
        )

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('repo_card'):
            info_card('REPO', self.repo or '--')
        with layout.cell('maxage_card'):
            info_card('MAX AGE', format_duration(self.max_age))
        with layout.cell('state_card'):
            state_label = info_card('CURRENT STATE', '--')
        with layout.cell('history'):
            with ui.row().classes('gap-4'):
                age_label = info_card('LAST ARCHIVE', '--')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table'](title='LOGS', limit=100, full_height=True)

        def update():
            m = self.latest_metric('last_backup_epoch')
            epoch_val = m.value if m else None

            if epoch_val is None:
                state_label.text = 'UNKNOWN'
                state_label.style(f"color: {STATUS_COLORS['offline']}")
                return

            epoch = int(epoch_val)
            if epoch == 0:
                state_label.text = 'NO ARCHIVES'
                state_label.style(f"color: {STATUS_COLORS['failed']}")
                age_label.text = 'Never'
                age_label.style(f"color: {STATUS_COLORS['failed']}")
                return

            age = int(time.time()) - epoch
            is_fresh = age <= self.max_age
            state_label.text = 'OK' if is_fresh else 'STALE'
            state_label.style(f"color: {STATUS_COLORS['online' if is_fresh else 'failed']}")
            age_label.text = format_age(age)
            age_label.style(f"color: {STATUS_COLORS['online' if is_fresh else 'failed']}")

        update()
        ui.timer(5.0, update)

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def get_actions(self) -> List[Dict[str, str]]:
        return []

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False
