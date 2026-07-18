import json
import re
import shlex
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.time_utils import parse_duration, format_duration, format_age
from vigil.core.modules.controllers.job_controller import JobRejected
from vigil.core.ui.components import info_card, safe_timer


_DEFAULT_LAYOUT = [
    ['host_card', 'repo_card', 'maxage_card', 'state_card'],
    ['size_card', 'dedup_card', 'count_card', 'age_card'],
    ['archives'],
    ['jobs'],
    ['logs'],
]


def _as_list(value: Any) -> List[str]:
    """
    Normalise a config value that may be a single string or a list of strings.

    YAML makes `exclude: "/tmp"` and `exclude: ["/tmp"]` equally natural to
    write. Without this, the string form would be iterated character by
    character, producing four bogus exclusion patterns instead of one — a
    failure that only shows up as a wrong backup.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _format_bytes(size: float) -> str:
    """Render a byte count as a human-readable size (e.g. "1.4 TB")."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(size) < 1024.0 or unit == 'TB':
            return f"{size:.1f} {unit}" if unit != 'B' else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TB"


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
    # Checked before the generic "permission denied" case below: an ssh:// repo
    # failure says "Permission denied (publickey)", which that case would
    # misread as a local file-permission problem and advise `require_sudo` —
    # useless when sudo is already on and the real fault is the identity borg
    # offered on its own hop to the repo server.
    if "permission denied (publickey)" in text or "publickey" in text:
        return ("Hint: borg could not authenticate to the repo server — set "
                "`ssh_key` to a private key on that host which the borg server "
                "authorizes (borg makes its own SSH connection, so Vigil's own "
                "login key does not apply).")
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
      collect_stats      Also run `borg info --json` each poll to record repo
                         size and deduplication metrics (default: true). Costs a
                         second round trip; set false to keep the poll minimal.
      ssh_key            Path (on the host borg runs on) to the SSH private key
                         borg should use to reach an ssh:// repo. Needed whenever
                         `repo` is an ssh:// URL: borg opens its own connection
                         from that host to the borg server, and without an
                         explicit identity it offers the invoking user's default
                         keys — under `require_sudo` that is root's, which the
                         borg server usually does not authorize. Ignored for a
                         local-path repo.
      rsh                Full replacement for the command borg uses to reach the
                         repo server (exported as BORG_RSH), for cases `ssh_key`
                         cannot express, e.g. a jump host. Takes precedence over
                         `ssh_key`.
      ssh_config.host    Host to run borg on (via BasePlugin's SSH config).

    Backup config (used by the "Run Backup" action; monitoring works without it):
      source_paths       List of paths to back up, e.g. ["/home", "/etc"].
                         Required to enable the backup action.
      exclude            List of exclusion patterns passed as --exclude, e.g.
                         ["/home/*/.cache", "*.tmp"]. Borg pattern syntax.
      exclude_from       Path to a file of exclusion patterns on the remote host
                         (borg's --exclude-from). Combined with `exclude`.
      exclude_caches     Skip directories tagged CACHEDIR.TAG (default: true).
      exclude_if_present List of marker filenames; a directory containing one is
                         skipped, e.g. [".nobackup"].
      one_file_system    Do not cross filesystem boundaries (default: true).
                         Prevents a source like "/" pulling in every mount.
      compression        Borg compression spec (default: "zstd"). e.g. "lz4",
                         "zstd,10", "none".
      archive_prefix     Name prefix for created archives (default: the monitor
                         name). Archives are named "<prefix>-<UTC timestamp>".
      cache_dir          BORG_BASE_DIR for backups, on the remote host
                         (default: "/var/cache/vigil-borg"). Must be writable and
                         persistent — a throwaway dir would force borg to rebuild
                         its chunk cache every run, making each backup a full
                         re-read rather than an incremental one.
      backup_lock_wait   Seconds a backup waits for the repo lock (default: 600).
                         Higher than `lock_wait` because a backup should queue
                         behind a concurrent operation rather than give up.

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
        # 30s rather than a few seconds: the read uses --bypass-lock, so this
        # only applies where borg still needs the lock briefly, and a repo busy
        # with its own maintenance can take longer than 5s to answer.
        self.lock_wait = config.get('lock_wait', 30)
        self.require_sudo = bool(config.get('require_sudo', False))
        # Clamp to >=1: --last 0 makes borg return every archive in the repo,
        # which on a long-retained repo is a needlessly huge poll.
        self.list_archives = max(1, int(config.get('list_archives', 10)))
        self.collect_stats = bool(config.get('collect_stats', True))
        self.ssh_key = config.get('ssh_key')
        self.rsh = config.get('rsh')

        # -- Backup configuration -------------------------------------------
        # A single string is accepted for the list-valued options because a
        # one-path backup is common and quoting it as a list is easy to forget;
        # silently treating "/home" as the 5 characters of a list would be a
        # confusing failure.
        self.source_paths = _as_list(config.get('source_paths'))
        self.exclude = _as_list(config.get('exclude'))
        self.exclude_from = config.get('exclude_from')
        self.exclude_caches = bool(config.get('exclude_caches', True))
        self.exclude_if_present = _as_list(config.get('exclude_if_present'))
        self.one_file_system = bool(config.get('one_file_system', True))
        self.compression = config.get('compression', 'zstd')
        self.archive_prefix = config.get('archive_prefix', name)
        self.cache_dir = config.get('cache_dir', '/var/cache/vigil-borg')
        self.backup_lock_wait = config.get('backup_lock_wait', 600)

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

    def _env_prefix(self, persistent_cache: bool = False) -> List[str]:
        """
        Build the environment assignments that precede every borg invocation.

        Any passphrase is passed as an environment prefix so it never appears in
        argv (which is world-visible via `ps`).

        A `passphrase_file` is read here, on the Vigil host, and its contents are
        inlined as BORG_PASSPHRASE — so the secret only ever lives on the machine
        Vigil runs on, and the monitored host needs no local copy. It still
        crosses to the remote host, but inside the encrypted SSH channel and as
        an env prefix (not argv), so it stays out of the remote `ps` listing.

        `persistent_cache` selects where borg keeps its config/cache/security
        dirs. Read-only polls use a throwaway temp dir (see below); a backup
        wants `cache_dir` instead, because rebuilding the chunk cache from
        scratch on every run turns an incremental backup into a full repository
        scan.
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

        if self.rsh or self.ssh_key:
            # For an ssh:// repo, borg opens its OWN connection from the host it
            # runs on to the borg server — separate from the SSH Vigil used to
            # get here, and with its own identity. Without this it falls back to
            # the invoking user's default keys, which for a `sudo` borg means
            # root's — usually not the key the borg server authorizes, giving
            # "Permission denied (publickey)". BORG_RSH is borg's equivalent of
            # borgmatic's `ssh_command`.
            rsh = self.rsh or (
                "ssh -i " + shlex.quote(self.ssh_key) +
                # The identity must be used exactly as given: without
                # IdentitiesOnly an agent or default key can be offered first
                # and the server may reject the connection before this key is
                # tried. BatchMode keeps a missing key a fast error, never a
                # prompt that would hang the poll.
                " -o IdentitiesOnly=yes -o BatchMode=yes"
            )
            env.append("BORG_RSH=" + shlex.quote(rsh))

        if persistent_cache and self.cache_dir:
            # A stable base dir lets borg reuse its chunk cache between backups,
            # which is what makes a repeat backup incremental rather than a full
            # re-read of every source file.
            env.append("BORG_BASE_DIR=" + shlex.quote(self.cache_dir))
        else:
            # Point borg's config/cache/security dirs at a fresh writable temp dir on
            # the remote host. Vigil often logs in as a locked-down system account
            # whose home is /var/empty (non-writable), where borg otherwise dies with
            # "Operation not permitted: '/var/empty/.config'" before it reads a single
            # archive — surfacing as a red monitor with no useful log. BORG_BASE_DIR
            # relocates all three dirs (~/.config/borg, ~/.cache/borg, ~/.config/borg/
            # security) under it. A throwaway dir is fine for a read-only list; the
            # small cost is borg can't reuse its chunk cache between polls.
            env.append("BORG_BASE_DIR=\"$(mktemp -d)\"")
        return env

    def _build(self, args: List[str], persistent_cache: bool = False) -> str:
        """
        Assemble a full remote shell command from borg arguments.

        With `require_sudo`, the whole thing runs as `sudo VAR=... borg ...`.
        The env assignments must come *after* `sudo`, not before it: sudo scrubs
        the environment it inherits, so a leading `BORG_PASSPHRASE=... sudo borg`
        would silently drop the passphrase. Passing them as sudo's own
        VAR=value arguments sets them in the privileged process instead, and
        they still stay out of the remote `ps` listing (sudo hides them from
        the argv it shows).

        All arguments are shlex-quoted so repo paths/URLs with spaces are safe.
        """
        prefix = ["sudo", "-n"] if self.require_sudo else []
        env = self._env_prefix(persistent_cache=persistent_cache)
        return " ".join(prefix + env + [shlex.quote(a) for a in args])

    def _list_command(self) -> str:
        """
        Build the shell command run on the SSH host: query the newest archives
        as JSON.
        """
        return self._build([
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
        ])

    def _info_command(self) -> str:
        """
        Build `borg info --json --last N` for repository and per-archive stats.

        Separate from `list`: only `info` reports the repo's cache stats —
        original/compressed/deduplicated size — which is what makes the
        deduplication ratio and storage trend visible. `--last N` additionally
        returns a stats block per archive (original/compressed/deduplicated
        size, file count), which `borg list --json` does not carry at all: its
        archive entries hold only name/id/time. Asking for the same N as the
        list keeps sizes available for every archive the UI shows, without a
        third round trip.
        """
        return self._build([
            self.borg_bin, "info",
            "--json",
            "--last", str(self.list_archives),
            "--bypass-lock",
            "--lock-wait", str(self.lock_wait),
            self.repo,
        ])

    def _backup_command(self, archive_name: Optional[str] = None,
                        dry_run: bool = False) -> str:
        """
        Build the `borg create` command for a backup run.

        Uses --log-json so progress arrives as structured records the job runner
        can parse, rather than as human-formatted text that would have to be
        scraped. --stats makes borg print the archive's size summary on
        completion, which is what populates the post-backup metrics.

        The archive name defaults to `archive_prefix` plus a UTC timestamp;
        borg's own {now} placeholder is deliberately avoided so the name Vigil
        records matches the name borg creates even if the clocks differ.
        """
        name = archive_name or self.default_archive_name()
        args = [
            self.borg_bin, "create",
            "--log-json",
            "--progress",
            "--compression", self.compression,
        ]
        # --stats and --dry-run are mutually exclusive in borg; select rather
        # than adding then removing, so neither flag depends on the other's
        # position in the list.
        args.append("--dry-run" if dry_run else "--stats")
        if self.one_file_system:
            # Stops the backup at filesystem boundaries — without it a source
            # like / silently pulls in every mounted network share and bind mount.
            args.append("--one-file-system")
        if self.exclude_caches:
            args.append("--exclude-caches")
        if self.exclude_if_present:
            for marker in self.exclude_if_present:
                args += ["--exclude-if-present", marker]
        for pattern in self.exclude:
            args += ["--exclude", pattern]
        if self.exclude_from:
            args += ["--exclude-from", self.exclude_from]
        args += ["--lock-wait", str(self.backup_lock_wait)]
        args.append(f"{self.repo}::{name}")
        args += self.source_paths
        return self._build(args, persistent_cache=True)

    def default_archive_name(self) -> str:
        """
        Generate the archive name for a new backup: prefix + UTC timestamp.

        UTC rather than local time so archive names sort chronologically even
        across a DST transition, where local timestamps repeat an hour.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return f"{self.archive_prefix}-{stamp}"

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
        self._store_archives(stdout)

        self.db_metrics.metric('archive_count', float(archive_count))
        self.db_metrics.metric('last_backup_epoch', float(latest_epoch))

        if archive_count == 0 or latest_epoch == 0:
            self.db_logger.write("No archives in repository", level="WARNING")
            self.set_status('failed')
            # An empty repo has no size stats worth fetching; skip the second
            # round trip entirely.
            return

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

        # Stats last: the status decision is what the monitor exists for, and it
        # must not wait on an optional second round trip that may be slow.
        if self.collect_stats:
            await self._collect_repo_stats()

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

    async def _collect_repo_stats(self) -> None:
        """
        Run `borg info --json` and record repository size metrics.

        Failure here is deliberately non-fatal: the repo's freshness has already
        been established by the list call, and losing a size datapoint should
        not turn a healthy monitor red. It logs at WARNING and returns.
        """
        ret, stdout, stderr = await self.ssh_collector.fetch_output(self._info_command())
        if ret != 0:
            self.db_logger.write(
                f"borg info failed (exit {ret}): {(stderr or stdout).strip()[:200]}",
                level="WARNING",
            )
            return

        # Per-archive sizes ride along in the same response; fold them into the
        # cached list so the UI table can show a size per archive.
        self._merge_archive_sizes(stdout)

        stats = self._parse_stats(stdout)
        if not stats:
            self.db_logger.write("Could not parse borg info output", level="WARNING")
            return

        for key, value in stats.items():
            self.db_metrics.metric(key, float(value))

        original = stats.get('original_size', 0)
        deduplicated = stats.get('deduplicated_size', 0)
        if original and deduplicated:
            # How much smaller the repo is than the raw data it protects — the
            # single number that says whether dedup+compression are working.
            ratio = original / deduplicated
            self.db_metrics.metric('dedup_ratio', ratio)
            self.db_logger.write(
                f"Repo size: {_format_bytes(deduplicated)} on disk for "
                f"{_format_bytes(original)} of data ({ratio:.1f}x reduction)",
                level="INFO",
            )

    def _parse_stats(self, stdout: str) -> Dict[str, float]:
        """
        Parse `borg info --json` into a flat metric dict.

        Borg reports these under cache.stats, which covers the whole repository
        (not a single archive). Returns {} when the output is unparseable or
        lacks the stats block — older borg versions omit it when the cache has
        not been built.
        """
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}

        cache = data.get('cache')
        stats = cache.get('stats') if isinstance(cache, dict) else None
        if not isinstance(stats, dict):
            return {}

        out = {}
        for src, dest in (
            ('total_size', 'original_size'),
            ('total_csize', 'compressed_size'),
            ('unique_csize', 'deduplicated_size'),
            ('total_chunks', 'total_chunks'),
            ('total_unique_chunks', 'unique_chunks'),
        ):
            value = stats.get(src)
            if isinstance(value, (int, float)):
                out[dest] = float(value)
        return out

    def _parse_archive_sizes(self, stdout: str) -> Dict[str, Dict[str, float]]:
        """
        Parse per-archive stats out of `borg info --json --last N`.

        Returns {archive_name: {'original', 'compressed', 'deduplicated',
        'nfiles'}}. Only `info` carries these; `list --json` has no size fields
        at all. Returns {} on unparseable output or when the stats block is
        absent (older borg omits it before the cache is built).
        """
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}

        out = {}
        for archive in data.get('archives') or []:
            if not isinstance(archive, dict):
                continue
            name = archive.get('name')
            stats = archive.get('stats')
            if not name or not isinstance(stats, dict):
                continue
            entry = {}
            for src, dest in (
                ('original_size', 'original'),
                ('compressed_size', 'compressed'),
                ('deduplicated_size', 'deduplicated'),
                ('nfiles', 'nfiles'),
            ):
                value = stats.get(src)
                if isinstance(value, (int, float)):
                    entry[dest] = float(value)
            if entry:
                out[name] = entry
        return out

    def _merge_archive_sizes(self, stdout: str) -> None:
        """
        Attach per-archive sizes to the archive list cached for the UI.

        The list is captured from `borg list` earlier in the same poll; this
        re-reads that cached payload and rewrites it with sizes folded in, so
        the UI keeps reading one metric rather than joining two. A no-op when
        either side is missing — the table then shows names and ages only,
        which is what it did before sizes existed.
        """
        sizes = self._parse_archive_sizes(stdout)
        if not sizes:
            return
        archives, info = self.cached_archives()
        if not archives:
            return
        for archive in archives:
            entry = sizes.get(archive.get('name'))
            if entry:
                archive.update(entry)
        payload = json.dumps({'archives': archives, 'repository': info})
        self.db_metrics.metric('archive_list', float(len(archives)), metadata=payload)

    def _store_archives(self, stdout: str) -> None:
        """
        Cache the parsed archive list for the UI as a metric with JSON metadata.

        The UI needs the archive list to render a table, but it renders outside
        the polling cycle and must not run its own SSH command to do so — that
        would put a network round trip on every page refresh and every 5-second
        timer tick. Stashing the parsed result alongside the metric lets the UI
        read the last poll's view of the repo straight from the database.
        """
        archives, info = self._archive_details(stdout)
        if not archives:
            return
        payload = json.dumps({'archives': archives, 'repository': info})
        self.db_metrics.metric('archive_list', float(len(archives)), metadata=payload)

    def cached_archives(self) -> (List[Dict[str, Any]], Dict[str, Any]):
        """
        Read back the archive list stored by the last successful poll.

        Returns ([], {}) when nothing has been cached yet or the payload is
        unreadable, so the UI shows an empty table rather than raising.
        """
        metric = self.latest_metric('archive_list')
        if metric is None or not metric.metadata:
            return [], {}
        try:
            data = json.loads(metric.metadata)
        except (json.JSONDecodeError, ValueError):
            return [], {}
        if not isinstance(data, dict):
            return [], {}
        return data.get('archives') or [], data.get('repository') or {}

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
        with layout.cell('size_card'):
            size_label = info_card('REPO SIZE', '--')
        with layout.cell('dedup_card'):
            dedup_label = info_card('DEDUP RATIO', '--')
        with layout.cell('count_card'):
            count_label = info_card('ARCHIVES', '--')
        with layout.cell('age_card'):
            age_label = info_card('LAST ARCHIVE', '--')
        with layout.cell('archives'):
            archives_table = self._render_archives_table()
        with layout.cell('jobs'):
            update_jobs = self._render_jobs_panel()
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table'](title='LOGS', limit=100, full_height=True)

        def update():
            m = self.latest_metric('last_backup_epoch')
            epoch_val = m.value if m else None

            self._update_stat_cards(size_label, dedup_label, count_label)
            self._refresh_archives_table(archives_table)
            update_jobs()

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

    def _update_stat_cards(self, size_label, dedup_label, count_label) -> None:
        """Refresh the repository statistics cards from the latest metrics."""
        dedup_metric = self.latest_metric('deduplicated_size')
        size_label.text = (
            _format_bytes(dedup_metric.value) if dedup_metric else '--'
        )

        ratio_metric = self.latest_metric('dedup_ratio')
        dedup_label.text = f"{ratio_metric.value:.1f}x" if ratio_metric else '--'

        count_metric = self.latest_metric('archive_count')
        count_label.text = str(int(count_metric.value)) if count_metric else '--'

    def _render_archives_table(self):
        """
        Build the archive table: one row per archive with its name and age.

        Replaces dumping archives into the event log, where they were
        interleaved with status messages and could not be sorted or scanned.
        """
        from nicegui import ui
        from vigil.core.ui.components import card
        from vigil.core.ui.theme import PRIMARY

        with card('w-full'):
            ui.label('ARCHIVES').classes('font-bold mb-2').style(f'color: {PRIMARY}')
            return ui.table(
                columns=[
                    {'name': 'name', 'label': 'Archive', 'field': 'name',
                     'align': 'left', 'sortable': True},
                    {'name': 'created', 'label': 'Created', 'field': 'created',
                     'align': 'left', 'sortable': True},
                    {'name': 'age', 'label': 'Age', 'field': 'age', 'align': 'left'},
                    # Size of the source data this archive captured.
                    {'name': 'size', 'label': 'Size', 'field': 'size',
                     'align': 'right', 'sortable': True},
                    # What this archive actually added to the repo — near zero
                    # for an incremental run, which is the number that explains
                    # why the repo is not growing by `size` every backup.
                    {'name': 'added', 'label': 'Added', 'field': 'added',
                     'align': 'right', 'sortable': True},
                    {'name': 'files', 'label': 'Files', 'field': 'files',
                     'align': 'right', 'sortable': True},
                ],
                rows=[],
                row_key='name',
            ).classes('w-full border-none')

    def _refresh_archives_table(self, table) -> None:
        """Repopulate the archive table from the last poll's cached list."""
        archives, _ = self.cached_archives()
        now = int(time.time())
        table.rows = [
            {
                'name': a.get('name', '?'),
                'created': (
                    datetime.fromtimestamp(a['epoch']).strftime('%Y-%m-%d %H:%M')
                    if a.get('epoch') else 'unknown'
                ),
                'age': format_age(now - a['epoch']) if a.get('epoch') else 'unknown',
                # Sizes arrive from `borg info`, a separate call than the one
                # that produced the names; '--' covers a poll where stats are
                # disabled or the info call failed.
                'size': _format_bytes(a['original']) if 'original' in a else '--',
                'added': (
                    _format_bytes(a['deduplicated']) if 'deduplicated' in a else '--'
                ),
                'files': f"{int(a['nfiles']):,}" if 'nfiles' in a else '--',
            }
            for a in archives
        ]
        table.update()

    def _render_jobs_panel(self):
        """
        Build the job panel: run/cancel controls, live progress, and history.

        Returns an update callable the page timer drives, so job state stays
        current without the panel owning a second timer.
        """
        from nicegui import ui
        from vigil.core.ui.components import card
        from vigil.core.ui.theme import PRIMARY, STATUS_COLORS

        with card('w-full'):
            with ui.row().classes('w-full items-center justify-between mb-2'):
                ui.label('BACKUP JOBS').classes('font-bold').style(f'color: {PRIMARY}')
                with ui.row().classes('gap-2'):
                    run_btn = ui.button(
                        'Run Backup', icon='play_arrow',
                        on_click=lambda: self._start_backup_from_ui(),
                    ).props('dense')
                    cancel_btn = ui.button(
                        'Cancel', icon='stop',
                        on_click=lambda: self._cancel_backup_from_ui(),
                    ).props('dense outline color=negative')

            progress_label = ui.label('').classes('text-xs font-mono mb-2')

            jobs_table = ui.table(
                columns=[
                    {'name': 'started', 'label': 'Started', 'field': 'started', 'align': 'left'},
                    {'name': 'kind', 'label': 'Kind', 'field': 'kind', 'align': 'left'},
                    {'name': 'state', 'label': 'State', 'field': 'state', 'align': 'left'},
                    {'name': 'duration', 'label': 'Duration', 'field': 'duration', 'align': 'left'},
                ],
                rows=[],
                row_key='id',
            ).classes('w-full border-none')

            def update():
                running = self.job_controller.is_running()
                # Backups need somewhere to back up from; without source_paths
                # the button would only ever produce a config error.
                run_btn.set_enabled(bool(self.source_paths) and not running)
                cancel_btn.set_visibility(running)

                if running:
                    job = self.db.get_job(self.job_controller.current_job_id())
                    progress = (job or {}).get('progress') or 'Starting...'
                    progress_label.text = progress
                    progress_label.style(f"color: {STATUS_COLORS['online']}")
                elif not self.source_paths:
                    progress_label.text = 'No source_paths configured — backups disabled'
                    progress_label.style(f"color: {STATUS_COLORS['offline']}")
                else:
                    progress_label.text = ''

                jobs_table.rows = [
                    {
                        'id': j['id'],
                        'started': j['started'],
                        'kind': j['kind'],
                        'state': j['state'],
                        'duration': format_duration(j['duration']),
                    }
                    for j in self.job_controller.recent(limit=10)
                ]
                jobs_table.update()

            update()
            return update

    def _start_backup_from_ui(self) -> None:
        """Kick off a backup from the UI button, reporting the outcome."""
        from nicegui import ui
        import asyncio

        if not self.source_paths:
            ui.notify('No source_paths configured for this monitor', type='negative')
            return
        if self.job_controller.is_running():
            ui.notify('A job is already running', type='warning')
            return

        ui.notify('Backup started', type='positive')
        # Fire-and-forget: the job outlives this request, and its state is in
        # the DB, so the panel picks it up on the next timer tick rather than
        # this handler waiting hours for a result.
        asyncio.create_task(self.on_action('run_backup'))

    def _cancel_backup_from_ui(self) -> None:
        """Request cancellation of the running job."""
        from nicegui import ui
        if self.job_controller.cancel():
            ui.notify('Cancellation requested', type='warning')
        else:
            ui.notify('No job is running', type='info')

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def get_actions(self) -> List[Dict[str, str]]:
        # Backup is only offered when there is something to back up; exposing it
        # without source_paths would present a button that can only fail.
        if not self.source_paths:
            return []
        return [
            {'name': 'Run Backup', 'action_id': 'run_backup',
             'variant': 'primary', 'icon': 'backup'},
            {'name': 'Dry Run', 'action_id': 'dry_run_backup',
             'variant': 'secondary', 'icon': 'fact_check'},
        ]

    async def on_action(self, action_id: str, **kwargs) -> bool:
        if action_id in ('run_backup', 'dry_run_backup'):
            return await self._run_backup(dry_run=(action_id == 'dry_run_backup'))
        return False

    async def _run_backup(self, dry_run: bool = False) -> bool:
        """
        Run `borg create` as a tracked job, streaming progress into the DB.

        Returns True only when borg exits 0. The command is redacted before
        being persisted with the job, because it carries the inlined passphrase
        and the job row is shown in the UI and retained after completion.
        """
        if not self.repo:
            self.db_logger.write("Cannot back up: no 'repo' configured", level="ERROR")
            return False
        if not self.source_paths:
            self.db_logger.write(
                "Cannot back up: no 'source_paths' configured", level="ERROR"
            )
            return False

        kind = 'dry-run' if dry_run else 'backup'
        command = self._backup_command(dry_run=dry_run)
        self.db_logger.write(
            f"Starting {kind}: {_redact(command)}", level="INFO"
        )

        # on_line fires while run_job is still executing, so it cannot close over
        # run_job's return value — the id must be resolved from the controller,
        # which sets it before the first line of output can arrive.
        def on_line(stream: str, text: str) -> None:
            self._handle_backup_line(self.job_controller.current_job_id(), stream, text)

        try:
            _job_id, exit_code = await self.job_controller.run_job(
                kind=kind,
                command=command,
                redacted=_redact(command),
                on_line=on_line,
            )
        except JobRejected as e:
            self.db_logger.write(str(e), level="WARNING")
            return False

        # Replace the last streamed progress line with a final summary. A short
        # backup can finish before borg emits any counter-bearing progress
        # record, which would otherwise leave the panel blank or mid-file.
        self.db.set_job_progress(
            _job_id,
            f"{kind.capitalize()} completed" if exit_code == 0
            else f"{kind.capitalize()} finished (exit {exit_code})",
        )

        if exit_code == 0:
            self.db_logger.write(f"{kind.capitalize()} completed successfully", level="INFO")
            return True

        # Borg exits 1 for warnings (e.g. a file vanished mid-backup) and >=2
        # for real errors. A warning still produced a valid archive, so it is
        # not reported as a failure.
        if exit_code == 1:
            self.db_logger.write(
                f"{kind.capitalize()} completed with warnings (exit 1)", level="WARNING"
            )
            return True

        self.db_logger.write(f"{kind.capitalize()} failed (exit {exit_code})", level="ERROR")
        return False

    def _handle_backup_line(self, job_id: Optional[int], stream: str, text: str) -> None:
        """
        Interpret one line of borg --log-json output.

        borg emits one JSON object per line: archive_progress records carry the
        running file/byte counts, while log_message records carry warnings and
        errors. Non-JSON lines occur too (ssh banners, tracebacks) and are left
        alone — they are still stored as raw output by the job runner.
        """
        if job_id is None or not text.startswith('{'):
            return
        try:
            record = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(record, dict):
            return

        rec_type = record.get('type')
        if rec_type == 'archive_progress':
            # borg brackets a run with two counter-less progress records: the
            # first before anything is read, and a bare {"finished": true} at
            # the end. Persisting either overwrites the real totals, leaving a
            # completed backup reading "0 files, 0 B read" — which is what the
            # user is left staring at, since it is the last write to land.
            if record.get('finished'):
                return
            original = record.get('original_size') or 0
            deduplicated = record.get('deduplicated_size') or 0
            nfiles = record.get('nfiles') or 0
            if not (original or deduplicated or nfiles):
                return
            path = record.get('path') or ''
            summary = (
                f"{nfiles} files, {_format_bytes(original)} read, "
                f"{_format_bytes(deduplicated)} new"
            )
            if path:
                summary += f" — {path}"
            self.db.set_job_progress(job_id, summary)
        elif rec_type == 'log_message':
            level = (record.get('levelname') or 'INFO').upper()
            message = record.get('message') or ''
            if message and level in ('WARNING', 'ERROR', 'CRITICAL'):
                self.db_logger.write(f"borg: {message}", level=level)
