import json
import re
import shlex
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.time_utils import parse_duration, format_duration, format_age
from vigil.core.ui.components import info_card, safe_timer


_DEFAULT_LAYOUT = [
    ['host_card', 'repo_card', 'maxage_card', 'state_card'],
    ['history'],
    ['logs'],
]


def _redact(command: str) -> str:
    """
    Mask secrets in a borg command so it can be written to the event log.

    `_list_command` inlines the repo passphrase as BORG_PASSPHRASE=<secret>, so
    the raw string must never reach the database. Replaces the value (quoted or
    bare) with `*****`, leaving the rest of the command readable for debugging.
    BORG_PASSCOMMAND is masked too — it is a shell command, not the secret
    itself, but it routinely names a key path worth keeping out of the log.
    """
    return re.sub(
        r"(BORG_PASS(?:PHRASE|COMMAND)=)('(?:[^']|'\\'')*'|\"[^\"]*\"|\S+)",
        r"\1*****",
        command,
    )


def _failure_hint(stderr: str) -> Optional[str]:
    """
    Map a borg/sudo failure to a one-line hint at the likely cause.

    borg's own errors say what happened but not what to change, and these few
    account for most first-time setup failures. Returns None when nothing
    matches, so the raw error stands alone rather than being second-guessed.
    """
    text = (stderr or "").lower()
    if "command not found" in text:
        return ("Hint: the borg binary is not on PATH for that user — under sudo "
                "it must be on root's PATH too (set `borg_bin` to an absolute path).")
    if "a password is required" in text or "sudo: a terminal is required" in text:
        return ("Hint: sudo needs a password — grant the SSH user passwordless "
                "sudo for borg (NOPASSWD).")
    if "not allowed to set the following environment variables" in text:
        return ("Hint: sudoers forbids setting BORG_PASSPHRASE — the rule needs "
                "the SETENV tag to pass the passphrase through sudo.")
    if "passphrase" in text or "not a valid repository" in text:
        return ("Hint: the repo is encrypted and the passphrase was missing or "
                "wrong — check `passphrase_file` / `passphrase_command`.")
    if "permission denied" in text:
        return ("Hint: the SSH user cannot read the repo — add it to the repo's "
                "group or set `require_sudo: true`.")
    if "does not exist" in text or "no such file" in text:
        return "Hint: the `repo` path does not exist on that host."
    if "failed to create/acquire the lock" in text:
        return ("Hint: the repo is locked by another borg process — a backup may "
                "be running.")
    return None


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
      passphrase_file    Path to a file on the Vigil host whose contents are the
                         passphrase. Read locally each poll and exported as
                         BORG_PASSPHRASE in the (remote) borg command, so the
                         secret only lives on the machine Vigil runs on — the
                         monitored host never needs a copy. Ideal for a sops- or
                         agenix-managed secret. Optional.
      passphrase_command A shell command whose stdout is the passphrase (exported
                         as BORG_PASSCOMMAND, e.g. "cat /etc/vigil/repo.pass").
                         Runs on the *remote* borg host, so the file must exist
                         there. Optional.
      borg_bin           Path to the borg executable. Defaults to "borg".
      lock_wait          Seconds borg waits for a repo lock before giving up, so a
                         concurrent backup does not hang the poll. Defaults to 5.
      list_archives      How many recent archives to fetch and log each poll
                         (default: 10). The newest is what drives status; the
                         rest are logged for visibility into the repo's
                         contents. Set to 1 to keep the poll minimal.
      require_sudo       Run borg via sudo on the remote host (default: false).
                         Needed when the repo is only readable by root. Requires
                         passwordless sudo for the borg binary, e.g.
                         "vigil ALL=(ALL) NOPASSWD: /run/current-system/sw/bin/borg".
      ssh_config.host    Host to run borg on (via BasePlugin's SSH config).

    Passphrase precedence when more than one is set: `passphrase` >
    `passphrase_file` > `passphrase_command`.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.repo = config.get('repo')
        self.max_age = parse_duration(config.get('max_age', '1d'))
        self.passphrase = config.get('passphrase')
        self.passphrase_file = config.get('passphrase_file')
        self.passphrase_command = config.get('passphrase_command')
        self.borg_bin = config.get('borg_bin', 'borg')
        self.lock_wait = config.get('lock_wait', 5)
        self.require_sudo = bool(config.get('require_sudo', False))
        # Clamp to >=1: --last 0 makes borg return every archive in the repo,
        # which on a long-retained repo is a needlessly huge poll.
        self.list_archives = max(1, int(config.get('list_archives', 10)))

    # -------------------------------------------------------------------------
    # Collection
    # -------------------------------------------------------------------------

    def _read_passphrase_file(self) -> Optional[str]:
        """
        Read the passphrase from `passphrase_file` on the local (Vigil) host.

        The file is read fresh on every poll so a rotated secret is picked up
        without restarting Vigil. A trailing newline (as `echo`/editors add) is
        stripped. Returns None and logs on any read error, letting the caller
        fall through to no passphrase (borg then fails clearly rather than the
        poll raising).
        """
        try:
            with open(self.passphrase_file, "r") as f:
                return f.read().rstrip("\n")
        except OSError as e:
            self.db_logger.write(
                f"Could not read passphrase_file {self.passphrase_file!r}: {e}",
                level="ERROR",
            )
            return None

    def _list_command(self) -> str:
        """
        Build the shell command run on the SSH host: query the newest archive as
        JSON. Any passphrase is passed as an environment prefix so it never
        appears in argv (which is world-visible via `ps`).

        A `passphrase_file` is read here, on the Vigil host, and its contents are
        inlined as BORG_PASSPHRASE — so the secret only ever lives on the machine
        Vigil runs on, and the monitored host needs no local copy. It still
        crosses to the remote host, but inside the encrypted SSH channel and as
        an env prefix (not argv), so it stays out of the remote `ps` listing.

        With `require_sudo`, the whole thing runs as `sudo VAR=... borg ...`.
        The env assignments must come *after* `sudo`, not before it: sudo scrubs
        the environment it inherits, so a leading `BORG_PASSPHRASE=... sudo borg`
        would silently drop the passphrase. Passing them as sudo's own
        VAR=value arguments sets them in the privileged process instead, and
        they still stay out of the remote `ps` listing (sudo hides them from
        the argv it shows).

        All arguments are shlex-quoted so repo paths/URLs with spaces are safe.
        """
        env = []
        if self.passphrase is not None:
            env.append("BORG_PASSPHRASE=" + shlex.quote(self.passphrase))
        elif self.passphrase_file is not None:
            secret = self._read_passphrase_file()
            if secret is not None:
                env.append("BORG_PASSPHRASE=" + shlex.quote(secret))
        elif self.passphrase_command is not None:
            env.append("BORG_PASSCOMMAND=" + shlex.quote(self.passphrase_command))
        # Never prompt interactively — fail fast instead of hanging the poll.
        env.append("BORG_RELOCATED_REPO_ACCESS_IS_OK=no")
        # Point borg's config/cache/security dirs at a fresh writable temp dir on
        # the remote host. Vigil often logs in as a locked-down system account
        # whose home is /var/empty (non-writable), where borg otherwise dies with
        # "Operation not permitted: '/var/empty/.config'" before it reads a single
        # archive — surfacing as a red monitor with no useful log. BORG_BASE_DIR
        # relocates all three dirs (~/.config/borg, ~/.cache/borg, ~/.config/borg/
        # security) under it. A throwaway dir is fine for a read-only list; the
        # small cost is borg can't reuse its chunk cache between polls.
        env.append("BORG_BASE_DIR=\"$(mktemp -d)\"")

        args = [
            self.borg_bin, "list",
            "--last", str(self.list_archives),
            "--json",
            # Read-only health check: skip lock acquisition entirely. Vigil
            # typically reads a repo it can traverse but not write (e.g. the
            # `borg` group on a 0750 repo), where taking borg's normal lock —
            # which writes lock.exclusive into the repo dir — fails with
            # "Permission denied" and the poll never reads an archive. Bypassing
            # the lock also means a concurrent backup can't block the poll.
            "--bypass-lock",
            "--lock-wait", str(self.lock_wait),
            self.repo,
        ]
        prefix = ["sudo", "-n"] if self.require_sudo else []
        return " ".join(prefix + env + [shlex.quote(a) for a in args])

    async def on_collect(self):
        if not self.repo:
            self.db_logger.write("No 'repo' configured for borg monitor", level="ERROR")
            self.set_status('failed')
            return

        command = self._list_command()
        # Redacted so the inlined passphrase never reaches the database.
        self.db_logger.write(f"Running: {_redact(command)}", level="INFO")

        ret, stdout, stderr = await self.ssh_collector.fetch_output(command)

        if ret != 0:
            detail = (stderr or stdout).strip()
            self.db_logger.write(
                f"borg list failed (exit {ret}): {detail}", level="ERROR"
            )
            hint = _failure_hint(detail)
            if hint:
                self.db_logger.write(hint, level="ERROR")
            self.set_status('failed')
            return

        latest_epoch, archive_count = self._newest_archive(stdout)

        if latest_epoch is None:
            self.db_logger.write(
                "Could not parse borg output — no archive timestamps found",
                level="ERROR"
            )
            # The raw output is the only way to tell a borg warning banner from
            # genuinely malformed JSON; truncated so a runaway dump can't flood
            # the log.
            snippet = (stdout or stderr or "").strip()[:500]
            if snippet:
                self.db_logger.write(f"Raw output was: {snippet}", level="ERROR")
            self.set_status('failed')
            return

        self._log_repo_details(stdout)

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

    def _archive_details(self, stdout: str) -> (List[Dict[str, Any]], Dict[str, Any]):
        """
        Parse the same output into (archives, repo_info) for logging.

        Kept separate from `_newest_archive` so the status decision stays driven
        by the one timestamp it needs, while this pulls the descriptive fields —
        archive names and their times, plus the repo's location, encryption mode
        and last-modified stamp. Returns ([], {}) on unparseable output; the
        caller has already failed the poll on that.

        Each returned archive is {'name': str, 'epoch': int}, newest first.
        """
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return [], {}

        if not isinstance(data, dict):
            return [], {}

        raw = data.get('archives') or []
        if not isinstance(raw, list):
            return [], {}

        archives = []
        for archive in raw:
            if not isinstance(archive, dict):
                continue
            archives.append({
                # Borg repeats the name as "archive"/"barchive"/"name"; any will do.
                'name': archive.get('name') or archive.get('archive') or '?',
                'epoch': _parse_archive_time(
                    archive.get('start') or archive.get('time', '')
                ),
            })
        archives.sort(key=lambda a: a['epoch'], reverse=True)

        repo = data.get('repository')
        info = {}
        if isinstance(repo, dict):
            info['location'] = repo.get('location') or ''
            info['last_modified'] = repo.get('last_modified') or ''
        enc = data.get('encryption')
        if isinstance(enc, dict):
            info['encryption'] = enc.get('mode') or ''

        return archives, info

    def _log_repo_details(self, stdout: str) -> None:
        """
        Write the descriptive repo/archive lines to the event log.

        One line summarising the repository, then one per archive returned
        (bounded by `list_archives`). Runs on every successful poll, so the log
        shows what the repo actually held at each check rather than only the
        derived status.
        """
        archives, info = self._archive_details(stdout)

        if info:
            parts = []
            if info.get('location'):
                parts.append(f"location={info['location']}")
            if info.get('encryption'):
                parts.append(f"encryption={info['encryption']}")
            if info.get('last_modified'):
                parts.append(f"last_modified={info['last_modified']}")
            if parts:
                self.db_logger.write("Repository: " + ", ".join(parts), level="INFO")

        if not archives:
            return

        self.db_logger.write(
            f"{len(archives)} most recent archive(s):", level="INFO"
        )
        for archive in archives:
            # epoch 0 means the timestamp was unparseable — say so rather than
            # printing a bogus 1970 age.
            age = (
                format_age(int(time.time()) - archive['epoch'])
                if archive['epoch'] else "unknown age"
            )
            self.db_logger.write(
                f"  {archive['name']} ({age})", level="INFO"
            )

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
        safe_timer(5.0, update)

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def get_actions(self) -> List[Dict[str, str]]:
        return []

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False
