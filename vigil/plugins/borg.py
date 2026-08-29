"""Borg backup repository freshness and size, collected by running borg over
SSH on the target: `borg list --json` each cycle, `borg info --json` when
collect_stats is on, and a detached `borg create` job that actions launch and
later cycles poll to completion. Config: repo, max_age, passphrase /
passphrase_file / passphrase_command, borg_bin, ssh_key / rsh, require_sudo,
list_archives, collect_stats, cache_dir, lock_wait / backup_lock_wait, and the
backup set (source_paths, exclude*, one_file_system, compression,
archive_prefix). A failed list, an empty repo, or a newest archive older than
max_age is failed; this monitor has no warning tier."""

import json
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import ActionPlan, CmdResult, Command, CollectResult
from vigil.core.connectors import ssh_connector as detached
from vigil.plugins.base.plugin_helpers import parse_duration, format_duration, format_age


_POLL_BASE_DIR_VAR = "__vigil_poll_base"

_DEFAULT_LAYOUT = [
    ['host_card', 'repo_card', 'maxage_card', 'state_card'],
    ['size_card', 'dedup_card', 'count_card', 'age_card'],
    ['archives'],
    ['jobs'],
    ['events'],
]


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _format_size(size: float) -> str:
    """Formats a raw byte count, unlike plugin_helpers.format_bytes which takes GB."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(size) < 1024.0 or unit == 'TB':
            return f"{size:.1f} {unit}" if unit != 'B' else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TB"


def _redact(command: str) -> str:
    return re.sub(
        r"(BORG_PASS(?:PHRASE|COMMAND)=)('(?:[^']|'\\'')*'|\"[^\"]*\"|\S+)",
        r"\1*****",
        command,
    )


def _failure_hint(stderr: str) -> Optional[str]:
    text = (stderr or "").lower()
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
    if not value:
        return 0
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0
    return int(dt.timestamp())


def _decode_json(stdout: str) -> Dict[str, Any]:
    """Decodes a borg --json payload, returning {} when it is not a JSON object."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class RepoView:
    """Decoded `borg list --json` output shared by the parse helpers."""
    valid: bool = False
    raw_count: int = 0
    newest_epoch: int = 0
    archives: List[Dict[str, Any]] = field(default_factory=list)
    info: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_stdout(cls, stdout: str) -> 'RepoView':
        """Parses the payload once, returning an invalid view on malformed output."""
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        raw = data.get('archives') or []
        if not isinstance(raw, list):
            return cls()

        archives = []
        for archive in raw:
            if not isinstance(archive, dict):
                continue
            archives.append({
                'name': archive.get('name') or archive.get('archive') or '?',
                'epoch': _parse_archive_time(
                    archive.get('start') or archive.get('time', '')
                ),
            })
        archives.sort(key=lambda a: a['epoch'], reverse=True)
        newest = max((a['epoch'] for a in archives if a['epoch'] > 0), default=0)

        info = {}
        repo = data.get('repository')
        if isinstance(repo, dict):
            info['location'] = repo.get('location') or ''
            info['last_modified'] = repo.get('last_modified') or ''
        enc = data.get('encryption')
        if isinstance(enc, dict):
            info['encryption'] = enc.get('mode') or ''

        return cls(valid=True, raw_count=len(raw), newest_epoch=newest,
                   archives=archives, info=info)


class Borg(Plugin):
    DEFAULT_TIMEOUT = 180.0

    def __init__(self, name: str, config: Dict[str, Any]):
        config = {'timeout': self.DEFAULT_TIMEOUT, **config}
        super().__init__(name, config)
        self.repo = config.get('repo')
        self.max_age = parse_duration(config.get('max_age', '1d'))
        self.passphrase = config.get('passphrase')
        self.passphrase_file = config.get('passphrase_file')
        self.passphrase_command = config.get('passphrase_command')
        self.borg_bin = config.get('borg_bin', 'borg')
        self.lock_wait = config.get('lock_wait', 30)
        self.require_sudo = bool(config.get('require_sudo', False))
        self.list_archives = max(1, int(config.get('list_archives', 10)))
        self.collect_stats = bool(config.get('collect_stats', True))
        self.ssh_key = config.get('ssh_key')
        self.rsh = config.get('rsh')

        self.source_paths = _as_list(config.get('source_paths'))
        self.exclude = _as_list(config.get('exclude'))
        self.exclude_from = config.get('exclude_from')
        self.exclude_caches = bool(config.get('exclude_caches', True))
        self.exclude_if_present = _as_list(config.get('exclude_if_present'))
        self.one_file_system = bool(config.get('one_file_system', True))
        self.compression = config.get('compression', 'zstd')
        self.archive_prefix = config.get('archive_prefix', name)
        self.cache_dir = config.get('cache_dir', '/var/cache/vigil-borg')
        # Only an explicitly configured dir is known-writable on a monitor-only target, so polls fall back to mktemp under the default
        self.cache_dir_configured = bool(config.get('cache_dir'))
        self.backup_lock_wait = config.get('backup_lock_wait', 600)

        from vigil.core.ui.spec import register_enabled_predicate
        self._has_sources_name = f'borg_has_sources_{self.id}'
        register_enabled_predicate(self._has_sources_name)(lambda p: bool(p.source_paths))

    def _read_passphrase_file(self) -> Optional[str]:
        try:
            with open(self.passphrase_file, "r") as f:
                return f.read().rstrip("\n")
        except OSError:
            return None

    def _env_prefix(self, persistent_cache: bool = False) -> List[str]:
        env = []
        if self.passphrase is not None:
            env.append("BORG_PASSPHRASE=" + shlex.quote(self.passphrase))
        elif self.passphrase_file is not None:
            secret = self._read_passphrase_file()
            if secret is not None:
                env.append("BORG_PASSPHRASE=" + shlex.quote(secret))
        elif self.passphrase_command is not None:
            env.append("BORG_PASSCOMMAND=" + shlex.quote(self.passphrase_command))
        env.append("BORG_RELOCATED_REPO_ACCESS_IS_OK=no")

        if self.rsh or self.ssh_key:
            rsh = self.rsh or (
                "ssh -i " + shlex.quote(self.ssh_key) +
                " -o IdentitiesOnly=yes -o BatchMode=yes"
            )
            env.append("BORG_RSH=" + shlex.quote(rsh))

        if persistent_cache and self.cache_dir:
            env.append("BORG_BASE_DIR=" + shlex.quote(self.cache_dir))
        else:
            env.append(f'BORG_BASE_DIR="${_POLL_BASE_DIR_VAR}"')
        return env

    def _build(self, args: List[str], persistent_cache: bool = False) -> str:
        prefix = ["sudo", "-n"] if self.require_sudo else []
        env = self._env_prefix(persistent_cache=persistent_cache)
        command = " ".join(prefix + env + [shlex.quote(a) for a in args])
        if persistent_cache and self.cache_dir:
            return command
        # The trap is what keeps the throwaway dir throwaway, since borg builds a full chunks cache in it on every poll
        return (
            f"{_POLL_BASE_DIR_VAR}=$(mktemp -d); "
            f"trap 'rm -rf \"${_POLL_BASE_DIR_VAR}\"' EXIT; "
            f"{command}"
        )

    def _list_command(self) -> str:
        return self._build([
            self.borg_bin, "list",
            "--last", str(self.list_archives),
            "--json",
            "--bypass-lock",
            "--lock-wait", str(self.lock_wait),
            self.repo,
        ], persistent_cache=self.cache_dir_configured)

    def _info_command(self) -> str:
        return self._build([
            self.borg_bin, "info",
            "--json",
            "--last", str(self.list_archives),
            "--bypass-lock",
            "--lock-wait", str(self.lock_wait),
            self.repo,
        ], persistent_cache=self.cache_dir_configured)

    def _backup_command(self, archive_name: Optional[str] = None,
                        dry_run: bool = False) -> str:
        name = archive_name or self.default_archive_name()
        args = [
            self.borg_bin, "create",
            "--log-json",
            "--progress",
            "--compression", self.compression,
        ]
        args.append("--dry-run" if dry_run else "--stats")
        if self.one_file_system:
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
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return f"{self.archive_prefix}-{stamp}"

    def commands(self) -> List[Command]:
        # While a backup is running, this monitor cycle polls the detached job
        # (a plain SSH command) instead of listing the repo — the same command
        # handler, no streaming channel.
        job = self._running_job()
        if job is not None:
            return [Command(detached.poll_command(job['workdir'], job['pid'], job['output_seq']))]
        if not self.repo:
            return []
        commands = [Command(self._list_command())]
        if self.collect_stats:
            commands.append(Command(self._info_command()))
        return commands

    def _running_job(self) -> Optional[dict]:
        job = self.jobs.running() if self.jobs else None
        if job and job.get('pid') and job.get('workdir'):
            return job
        return None

    def parse(self, results: List[CmdResult]) -> CollectResult:
        job = self._running_job()
        if job is not None:
            return self._parse_poll(job, results[0])

        if not self.repo:
            return CollectResult.failed("No 'repo' configured for borg monitor")

        list_result = results[0]
        stdout, stderr, ret = list_result.stdout, list_result.stderr, list_result.exit_code
        logs = [(f"Running: {_redact(self._list_command())}", "INFO")]

        if ret != 0:
            detail = (stderr or stdout).strip()
            logs.append((f"borg list failed (exit {ret}): {detail}", "ERROR"))
            hint = _failure_hint(detail)
            if hint:
                logs.append((hint, "ERROR"))
            return CollectResult(logs=logs, status='failed')

        view = RepoView.from_stdout(stdout)

        if not view.valid:
            logs.append(("Could not parse borg output — no archive timestamps found", "ERROR"))
            snippet = (stdout or stderr or "").strip()[:500]
            if snippet:
                logs.append((f"Raw output was: {snippet}", "ERROR"))
            return CollectResult(logs=logs, status='failed')

        logs.extend(self._repo_detail_logs(view))
        metrics = {'archive_count': float(view.raw_count), 'last_backup_epoch': float(view.newest_epoch)}
        metadata = {}
        if view.archives:
            metrics['archive_list'] = float(len(view.archives))
            metadata['archive_list'] = json.dumps({'archives': view.archives, 'repository': view.info})

        if view.raw_count == 0 or view.newest_epoch == 0:
            logs.append(("No archives in repository", "WARNING"))
            return CollectResult(metrics=metrics, metadata=metadata, logs=logs, status='failed')

        age = int(time.time()) - view.newest_epoch
        if age > self.max_age:
            logs.append((
                f"Last archive was {format_age(age)}, exceeds max_age of "
                f"{format_duration(self.max_age)}",
                "WARNING",
            ))
            status = 'failed'
        else:
            logs.append((f"Last archive {format_age(age)}", "INFO"))
            status = 'online'

        if self.collect_stats and len(results) > 1:
            stats_metrics, stats_metadata, stats_logs, merged_archives = self._parse_repo_stats(
                results[1], view.archives,
            )
            metrics.update(stats_metrics)
            metadata.update(stats_metadata)
            logs.extend(stats_logs)
            if merged_archives is not None:
                metrics['archive_list'] = float(len(merged_archives))
                metadata['archive_list'] = json.dumps({'archives': merged_archives, 'repository': view.info})

        return CollectResult(metrics=metrics, metadata=metadata, logs=logs, status=status)

    def _repo_detail_logs(self, view: RepoView) -> List[tuple]:
        archives, info = view.archives, view.info
        logs = []

        if info:
            parts = []
            if info.get('location'):
                parts.append(f"location={info['location']}")
            if info.get('encryption'):
                parts.append(f"encryption={info['encryption']}")
            if info.get('last_modified'):
                parts.append(f"last_modified={info['last_modified']}")
            if parts:
                logs.append(("Repository: " + ", ".join(parts), "INFO"))

        if not archives:
            return logs

        logs.append((f"{len(archives)} most recent archive(s):", "INFO"))
        for archive in archives:
            age = (
                format_age(int(time.time()) - archive['epoch'])
                if archive['epoch'] else "unknown age"
            )
            logs.append((f"  {archive['name']} ({age})", "INFO"))
        return logs

    def _parse_repo_stats(self, info_result: CmdResult, archives: List[Dict[str, Any]]):
        """Pure: parses `borg info` output. Returns
        (metrics, metadata, logs, merged_archives_or_None)."""
        ret, stdout, stderr = info_result.exit_code, info_result.stdout, info_result.stderr
        if ret != 0:
            return {}, {}, [(f"borg info failed (exit {ret}): {(stderr or stdout).strip()[:200]}", "WARNING")], None

        data = _decode_json(stdout)
        sizes = self._parse_archive_sizes(data)
        merged_archives = None
        if sizes and archives:
            merged_archives = [dict(a) for a in archives]
            for archive in merged_archives:
                entry = sizes.get(archive.get('name'))
                if entry:
                    archive.update(entry)

        stats = self._parse_stats(data)
        if not stats:
            return {}, {}, [("Could not parse borg info output", "WARNING")], merged_archives

        metrics = {key: float(value) for key, value in stats.items()}
        logs = []
        original = stats.get('original_size', 0)
        deduplicated = stats.get('deduplicated_size', 0)
        if original and deduplicated:
            ratio = original / deduplicated
            metrics['dedup_ratio'] = ratio
            logs.append((
                f"Repo size: {_format_size(deduplicated)} on disk for "
                f"{_format_size(original)} of data ({ratio:.1f}x reduction)",
                "INFO",
            ))
        return metrics, {}, logs, merged_archives

    def _parse_stats(self, data: Dict[str, Any]) -> Dict[str, float]:
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

    def _parse_archive_sizes(self, data: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
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

    def cached_archives(self) -> (List[Dict[str, Any]], Dict[str, Any]):
        metric = self.data.latest_metric('archive_list')
        if metric is None or not metric.metadata:
            return [], {}
        try:
            data = json.loads(metric.metadata)
        except (json.JSONDecodeError, ValueError):
            return [], {}
        if not isinstance(data, dict):
            return [], {}
        return data.get('archives') or [], data.get('repository') or {}

    def get_actions(self) -> List[Dict[str, str]]:
        if not self.source_paths:
            return []
        return [
            {'name': 'Run Backup', 'action_id': 'run_backup',
             'variant': 'primary', 'icon': 'backup'},
            {'name': 'Dry Run', 'action_id': 'dry_run_backup',
             'variant': 'secondary', 'icon': 'fact_check'},
        ]

    def plan_action(self, action_id: str, **kwargs):
        if action_id not in ('run_backup', 'dry_run_backup'):
            return None

        if not self.repo:
            return CollectResult.failed("Cannot back up: no 'repo' configured")
        if not self.source_paths:
            return CollectResult.failed("Cannot back up: no 'source_paths' configured")
        if self._running_job() is not None:
            return CollectResult.failed("A backup is already running for this monitor",
                                        level="WARNING", status=None)

        dry_run = action_id == 'dry_run_backup'
        kind = 'dry-run' if dry_run else 'backup'
        command = self._backup_command(dry_run=dry_run)
        # Name the on-target workdir before the Job row exists; interpret_action
        # records the pid the launch prints and creates the row.
        token = f"{self.id}-{int(time.time())}"
        workdir = detached.workdir_for(token)
        self._pending_launch = (kind, _redact(command), workdir)
        return ActionPlan(detached.launch_command(command, workdir))

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        if action_id not in ('run_backup', 'dry_run_backup'):
            return result.exit_code == 0

        kind, redacted, workdir = getattr(self, '_pending_launch', ('backup', '', ''))
        self._pending_launch = None

        pid = detached.parse_launch(result.stdout) if result.exit_code == 0 else None
        if pid is None:
            return CollectResult.failed(
                f"Failed to launch {kind}: {(result.stderr or result.stdout).strip()[:200]}")

        job_id = self.jobs.create(kind, redacted, workdir)
        self.jobs.set_pid(job_id, pid)
        return CollectResult(
            logs=[(f"{kind.capitalize()} started (pid {pid})", "INFO")],
            success=True,
        )

    def _parse_poll(self, job: dict, result: CmdResult) -> CollectResult:
        """Advance a running detached backup from one poll's output. Appends
        new output lines, updates the progress summary, and on completion
        finalizes the Job row and returns the interpreted outcome."""
        job_id = job['id']
        poll = detached.parse_poll(result.stdout)

        lines, consumed = detached.split_lines(poll.new_output)
        if lines:
            self.jobs.append_output(job_id, lines)
            self.jobs.bump_output_seq(job_id, job['output_seq'] + consumed)
            summary = self._progress_from_lines(lines)
            if summary:
                self.jobs.set_progress(job_id, summary)

        still_running = poll.exit_code is None and poll.alive
        if still_running:
            return CollectResult()  # nothing to persist for the monitor itself

        # Completed (exit file present) or the process vanished with no exit
        # file (target rebooted mid-job → treat as failed).
        kind = job['kind']
        if poll.exit_code is None:
            self.jobs.finish(job_id, 'failed', exit_code=-1,
                             error='Process ended without writing an exit code')
            return CollectResult.failed(f"{kind.capitalize()} ended unexpectedly")

        exit_code = poll.exit_code
        state = 'succeeded' if exit_code in (0, 1) else 'failed'
        self.jobs.finish(job_id, state, exit_code=exit_code,
                         error=None if state == 'succeeded' else f"Exited with status {exit_code}")

        if exit_code == 0:
            return CollectResult(logs=[(f"{kind.capitalize()} completed successfully", "INFO")], success=True)
        if exit_code == 1:
            return CollectResult(logs=[(f"{kind.capitalize()} completed with warnings (exit 1)", "WARNING")], success=True)
        return CollectResult.failed(f"{kind.capitalize()} failed (exit {exit_code})")

    @staticmethod
    def _progress_from_lines(lines: List[str]) -> Optional[str]:
        """Extract the latest human progress summary from borg --log-json
        archive_progress records in a batch of newly-read output lines."""
        summary = None
        for text in lines:
            if not text.startswith('{'):
                continue
            try:
                record = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(record, dict) or record.get('type') != 'archive_progress':
                continue
            if record.get('finished'):
                continue
            original = record.get('original_size') or 0
            deduplicated = record.get('deduplicated_size') or 0
            nfiles = record.get('nfiles') or 0
            if not (original or deduplicated or nfiles):
                continue
            path = record.get('path') or ''
            summary = f"{nfiles} files, {_format_size(original)} read, {_format_size(deduplicated)} new"
            if path:
                summary += f" — {path}"
        return summary

    def _epoch(self) -> Optional[float]:
        m = self.data.latest_metric('last_backup_epoch')
        return m.value if m is not None else None

    def _state_pair(self) -> (str, Optional[str]):
        """Returns the (text, color) pair for the current-state card."""
        epoch = self._epoch()
        if epoch is None:
            return 'UNKNOWN', 'offline'
        if int(epoch) == 0:
            return 'NO ARCHIVES', 'failed'
        age = int(time.time()) - int(epoch)
        return ('OK', 'online') if age <= self.max_age else ('STALE', 'failed')

    def _age_pair(self) -> (str, Optional[str]):
        """Returns the (text, color) pair for the last-archive card."""
        epoch = self._epoch()
        if epoch is None:
            return '--', 'failed'
        if int(epoch) == 0:
            return 'Never', 'failed'
        age = int(time.time()) - int(epoch)
        return format_age(age), 'online' if age <= self.max_age else 'failed'

    @property
    def _state_text(self) -> str:
        return self._state_pair()[0]

    @property
    def _state_color(self) -> Optional[str]:
        return self._state_pair()[1]

    @property
    def _last_archive_age_text(self) -> str:
        return self._age_pair()[0]

    @property
    def _last_archive_age_color(self) -> Optional[str]:
        return self._age_pair()[1]

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'repo_card': {'title': 'REPO', 'value': self.repo or '--'},
                'maxage_card': {'title': 'MAX AGE', 'value': format_duration(self.max_age)},
                'state_card': {'title': 'CURRENT STATE', 'value_attr': '_state_text',
                              'color_attr': '_state_color'},
                'size_card': {'metric': 'deduplicated_size', 'title': 'REPO SIZE', 'format': 'bytes_gb'},
                'dedup_card': {'metric': 'dedup_ratio', 'title': 'DEDUP RATIO', 'format': 'dedup_ratio'},
                'count_card': {'metric': 'archive_count', 'title': 'ARCHIVES', 'format': 'int'},
                'age_card': {'title': 'LAST ARCHIVE', 'value_attr': '_last_archive_age_text',
                            'color_attr': '_last_archive_age_color'},
            },
            'tables': {
                'archives': {
                    'row_key': 'name',
                    'rows_attr': '_archive_rows',
                    'columns': [
                        {'name': 'name', 'label': 'Archive', 'field': 'name', 'align': 'left', 'sortable': True},
                        {'name': 'created', 'label': 'Created', 'field': 'created', 'align': 'left', 'sortable': True},
                        {'name': 'age', 'label': 'Age', 'field': 'age', 'align': 'left'},
                        {'name': 'size', 'label': 'Size', 'field': 'size', 'align': 'right', 'sortable': True},
                        {'name': 'added', 'label': 'Added', 'field': 'added', 'align': 'right', 'sortable': True},
                        {'name': 'files', 'label': 'Files', 'field': 'files', 'align': 'right', 'sortable': True},
                    ],
                },
            },
            'job_panel': {
                'widget': 'jobs',
                'title': 'BACKUP JOBS',
                'run_action_id': 'run_backup', 'run_label': 'Run Backup', 'run_icon': 'play_arrow',
                'cancel_label': 'Cancel', 'cancel_icon': 'stop',
                'enabled_if': self._has_sources_name,
                'history_limit': 10,
            },
            'events': {'title': 'EVENTS', 'limit': 100, 'full_height': True},
        }

    @property
    def _archive_rows(self) -> List[Dict[str, Any]]:
        archives, _ = self.cached_archives()
        now = int(time.time())
        return [
            {
                'name': a.get('name', '?'),
                'created': (
                    datetime.fromtimestamp(a['epoch']).strftime('%Y-%m-%d %H:%M')
                    if a.get('epoch') else 'unknown'
                ),
                'age': format_age(now - a['epoch']) if a.get('epoch') else 'unknown',
                'size': _format_size(a['original']) if 'original' in a else '--',
                'added': (
                    _format_size(a['deduplicated']) if 'deduplicated' in a else '--'
                ),
                'files': f"{int(a['nfiles']):,}" if 'nfiles' in a else '--',
            }
            for a in archives
        ]

