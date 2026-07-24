import json
import re
import shlex
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Union

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult, JobPlan
from vigil.plugins.base.plugin_helpers import parse_duration, format_duration, format_age


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


def _format_bytes(size: float) -> str:
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


class Borg(Plugin):
    DEFAULT_TIMEOUT = 180.0

    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        config = {'timeout': self.DEFAULT_TIMEOUT, **config}
        super().__init__(name, config, db, ssh_pool)
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
            env.append("BORG_BASE_DIR=\"$(mktemp -d)\"")
        return env

    def _build(self, args: List[str], persistent_cache: bool = False) -> str:
        prefix = ["sudo", "-n"] if self.require_sudo else []
        env = self._env_prefix(persistent_cache=persistent_cache)
        return " ".join(prefix + env + [shlex.quote(a) for a in args])

    def _list_command(self) -> str:
        return self._build([
            self.borg_bin, "list",
            "--last", str(self.list_archives),
            "--json",
            "--bypass-lock",
            "--lock-wait", str(self.lock_wait),
            self.repo,
        ])

    def _info_command(self) -> str:
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
        if not self.repo:
            return []
        commands = [Command(self._list_command())]
        if self.collect_stats:
            commands.append(Command(self._info_command()))
        return commands

    def parse(self, results: List[CmdResult]) -> CollectResult:
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

        latest_epoch, archive_count = self._newest_archive(stdout)

        if latest_epoch is None:
            logs.append(("Could not parse borg output — no archive timestamps found", "ERROR"))
            snippet = (stdout or stderr or "").strip()[:500]
            if snippet:
                logs.append((f"Raw output was: {snippet}", "ERROR"))
            return CollectResult(logs=logs, status='failed')

        logs.extend(self._repo_detail_logs(stdout))
        archives, archive_info = self._archive_details(stdout)
        metrics = {'archive_count': float(archive_count), 'last_backup_epoch': float(latest_epoch)}
        metadata = {}
        if archives:
            metrics['archive_list'] = float(len(archives))
            metadata['archive_list'] = json.dumps({'archives': archives, 'repository': archive_info})

        if archive_count == 0 or latest_epoch == 0:
            logs.append(("No archives in repository", "WARNING"))
            return CollectResult(metrics=metrics, metadata=metadata, logs=logs, status='failed')

        age = int(time.time()) - latest_epoch
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
                results[1], archives, archive_info,
            )
            metrics.update(stats_metrics)
            metadata.update(stats_metadata)
            logs.extend(stats_logs)
            if merged_archives is not None:
                metrics['archive_list'] = float(len(merged_archives))
                metadata['archive_list'] = json.dumps({'archives': merged_archives, 'repository': archive_info})

        return CollectResult(metrics=metrics, metadata=metadata, logs=logs, status=status)

    def _newest_archive(self, stdout: str) -> (Optional[int], int):
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
            epoch = _parse_archive_time(archive.get('start') or archive.get('time', ''))
            if epoch > newest:
                newest = epoch

        return newest, len(archives)

    def _archive_details(self, stdout: str) -> (List[Dict[str, Any]], Dict[str, Any]):
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

    def _repo_detail_logs(self, stdout: str) -> List[tuple]:
        archives, info = self._archive_details(stdout)
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

    def _parse_repo_stats(self, info_result: CmdResult, archives: List[Dict[str, Any]],
                          archive_info: Dict[str, Any]):
        """Pure: parses `borg info` output. Returns
        (metrics, metadata, logs, merged_archives_or_None)."""
        ret, stdout, stderr = info_result.exit_code, info_result.stdout, info_result.stderr
        if ret != 0:
            return {}, {}, [(f"borg info failed (exit {ret}): {(stderr or stdout).strip()[:200]}", "WARNING")], None

        sizes = self._parse_archive_sizes(stdout)
        merged_archives = None
        if sizes and archives:
            merged_archives = [dict(a) for a in archives]
            for archive in merged_archives:
                entry = sizes.get(archive.get('name'))
                if entry:
                    archive.update(entry)

        stats = self._parse_stats(stdout)
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
                f"Repo size: {_format_bytes(deduplicated)} on disk for "
                f"{_format_bytes(original)} of data ({ratio:.1f}x reduction)",
                "INFO",
            ))
        return metrics, {}, logs, merged_archives

    def _parse_stats(self, stdout: str) -> Dict[str, float]:
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

    def cached_archives(self) -> (List[Dict[str, Any]], Dict[str, Any]):
        metric = self.storage.latest_metric('archive_list')
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

    def plan_action(self, action_id: str, **kwargs) -> Optional[Union[JobPlan, CollectResult]]:
        if action_id not in ('run_backup', 'dry_run_backup'):
            return None

        if not self.repo:
            return CollectResult.failed("Cannot back up: no 'repo' configured")
        if not self.source_paths:
            return CollectResult.failed("Cannot back up: no 'source_paths' configured")

        dry_run = action_id == 'dry_run_backup'
        kind = 'dry-run' if dry_run else 'backup'
        command = self._backup_command(dry_run=dry_run)
        return JobPlan(kind=kind, command=command, redacted=_redact(command))

    def job_on_line(self, action_id: str, **kwargs):
        if action_id not in ('run_backup', 'dry_run_backup'):
            return None

        def on_line(stream: str, text: str) -> None:
            self._handle_backup_line(self.network.current_job_id(), stream, text)
        return on_line

    def interpret_job(self, action_id: str, exit_code: int, **kwargs):
        kind = 'dry-run' if action_id == 'dry_run_backup' else 'backup'

        if exit_code == 0:
            return CollectResult(logs=[(f"{kind.capitalize()} completed successfully", "INFO")], success=True)
        if exit_code == 1:
            return CollectResult(
                logs=[(f"{kind.capitalize()} completed with warnings (exit 1)", "WARNING")],
                success=True,
            )
        return CollectResult.failed(f"{kind.capitalize()} failed (exit {exit_code})")

    def _handle_backup_line(self, job_id: Optional[int], stream: str, text: str) -> None:
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
                self.storage.apply(CollectResult(logs=[(f"borg: {message}", level)]))

    def _epoch(self) -> Optional[float]:
        m = self.storage.latest_metric('last_backup_epoch')
        return m.value if m is not None else None

    @property
    def _state_text(self) -> str:
        epoch = self._epoch()
        if epoch is None:
            return 'UNKNOWN'
        epoch = int(epoch)
        if epoch == 0:
            return 'NO ARCHIVES'
        age = int(time.time()) - epoch
        return 'OK' if age <= self.max_age else 'STALE'

    @property
    def _state_color(self) -> Optional[str]:
        epoch = self._epoch()
        if epoch is None:
            return 'offline'
        epoch = int(epoch)
        if epoch == 0:
            return 'failed'
        age = int(time.time()) - epoch
        return 'online' if age <= self.max_age else 'failed'

    @property
    def _last_archive_age_text(self) -> str:
        epoch = self._epoch()
        if epoch is None:
            return '--'
        if int(epoch) == 0:
            return 'Never'
        return format_age(int(time.time()) - int(epoch))

    @property
    def _last_archive_age_color(self) -> Optional[str]:
        epoch = self._epoch()
        if epoch is None or int(epoch) == 0:
            return 'failed'
        age = int(time.time()) - int(epoch)
        return 'online' if age <= self.max_age else 'failed'

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
                'refresh_interval': 2.0,
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
                'size': _format_bytes(a['original']) if 'original' in a else '--',
                'added': (
                    _format_bytes(a['deduplicated']) if 'deduplicated' in a else '--'
                ),
                'files': f"{int(a['nfiles']):,}" if 'nfiles' in a else '--',
            }
            for a in archives
        ]

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)
