"""Borg backup repository freshness and size, collected by running borg over
SSH on the target: `borg list --json` each cycle, `borg info --json` when
collect_stats is on, and a detached `borg create` job that actions launch and
later cycles poll to completion. The two poll calls go out as one command that
runs them in sequence and frames their output, because separate commands are
dispatched concurrently and would contend for the repo's chunks cache lock.
Config: repo, max_age, passphrase / passphrase_file / passphrase_command,
borg_bin, ssh_key / rsh, require_sudo, list_archives, collect_stats, cache_dir,
lock_wait / backup_lock_wait, the backup set (source_paths, exclude*,
one_file_system, compression, archive_prefix), and allow_purge / purge_match. A borg error, an empty repo, or
a newest archive older than max_age is failed; an unreachable host, a missing
borg binary, a repo the SSH user cannot read, a lock borg gave up waiting for,
or unparseable output is unavailable. This monitor has no warning tier.
Optional extras: a restore canary (canary_path, canary_interval) extracted from
the newest archive and compared with the live file; check freshness
(check_units, check_max_age) read from those units' journal; actions to
preview a prune (keep_*, prune_match), list an archive's folders for the
browser (browse_limit), restore paths into restore_dir, diff an archive with
the one before it, and run check, compact, prune and archive deletion as
detached jobs, the destructive ones only with allow_delete. With allow_purge,
Purge Excluded rewrites existing archives without whatever the exclude set now
drops and compacts the space away, and Purge Preview lists what that would be."""

import json
import shlex
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import ActionPlan, CmdResult, Command, CollectResult
from vigil.core.connectors import ssh_connector as detached
from vigil.plugins.base.plugin_helpers import (
    KILL_GRACE_SECONDS, format_age, format_duration, parse_duration,
)
from vigil.plugins.borg.checks import (
    _canary_verdict, _check_unit_script, _merge_unit, _observed_unit, _parse_check_units,
)
from vigil.plugins.borg.parsing import (
    RepoView, _LIST_FORMAT, _PRUNE_LINE, _children_filter, _decode_json, _format_size, _parse_listing,
)
from vigil.plugins.borg.shell import (
    _CUT_SHORT, _POLL_BASE_DIR_VAR, _POLL_DEADLINE_VAR, _POLL_LEFT_FN, _POLL_RC_VAR,
    _archive_member, _as_list, _failure_hint, _frame_line, _list_unavailable, _redact, _split_poll,
)


_KEEP_OPTIONS = ('keep_within', 'keep_last', 'keep_secondly', 'keep_minutely', 'keep_hourly',
                 'keep_daily', 'keep_weekly', 'keep_monthly', 'keep_yearly')

# Actions that start a detached job, which interpret_action records from the pid the launch prints.
_LAUNCHED = ('run_backup', 'dry_run_backup', 'restore_archive', 'check_repo', 'compact_repo',
             'prune_repo', 'delete_archive', 'purge_preview', 'purge_excluded')

# Keeps only the "Processing" lines and the top-most excluded path of each dropped tree from `borg recreate --list`, then exits with borg's own status.
_TOPMOST_EXCLUDED = (
    "awk '/^__vigil_rc=/ { rc = substr($0, 12); next } "
    "/^x / { p = substr($0, 3); if (top != \"\" && index(p, top \"/\") == 1) next; top = p; print; next } "
    "{ top = \"\"; print } END { exit rc }'"
)

# How many listed folders the browser keeps before forgetting the least recently used.
_LISTING_CACHE = 256

_DEFAULT_LAYOUT = [
    ['host_card', 'repo_card', 'maxage_card', 'state_card'],
    ['size_card', 'dedup_card', 'count_card', 'age_card'],
    ['canary_card', 'checks_card'],
    ['tools'],
    ['archives'],
    ['browser'],
    ['jobs'],
    ['events'],
]


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
        self.canary_path = config.get('canary_path')
        self.canary_interval = parse_duration(config.get('canary_interval', '1d'))
        self.check_units = _as_list(config.get('check_units'))
        self.check_max_age = parse_duration(config.get('check_max_age', '8d'))
        self.restore_dir = config.get('restore_dir', '/var/tmp/vigil-restore')
        self.browse_limit = max(1, int(config.get('browse_limit', 2000)))
        self.retention = {key: config[key] for key in _KEEP_OPTIONS if config.get(key) not in (None, '', 0)}
        self.prune_match = config.get('prune_match', f"{self.archive_prefix}-*")
        self.allow_delete = bool(config.get('allow_delete', False))
        self.check_verify_data = bool(config.get('check_verify_data', False))
        self.allow_purge = bool(config.get('allow_purge', False))
        # Unset means every archive, since archives Vigil created share the repo with the owning tool's under a different prefix.
        self.purge_match = config.get('purge_match')
        self._polling_job = None
        self._poll_calls: List[str] = []
        # Archives never change once written, so a folder listed once stays true until the archive is deleted.
        self._listings: 'OrderedDict[Tuple[str, str], List[Dict[str, Any]]]' = OrderedDict()

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

    def _build(self, args: List[str], persistent_cache: bool = False,
               bounded: bool = False, wrapped: bool = True) -> str:
        prefix = ["sudo", "-n"] if self.require_sudo else []
        env = self._env_prefix(persistent_cache=persistent_cache)
        # Polls only: a backup is launched detached precisely so it outlives the collect cycle, and must not inherit its deadline.
        # After env, because sudo reads leading VAR=val as assignments and takes the first non-assignment as the command to run.
        deadline = self._poll_deadline() if bounded else []
        command = " ".join(prefix + env + deadline + [shlex.quote(a) for a in args])
        if not wrapped or (persistent_cache and self.cache_dir):
            return command
        # The trap is what keeps the throwaway dir throwaway, since borg builds a full chunks cache in it on every poll
        return (
            f"{_POLL_BASE_DIR_VAR}=$(mktemp -d); "
            f"trap 'rm -rf \"${_POLL_BASE_DIR_VAR}\"' EXIT; "
            f"{command}"
        )

    def _poll_deadline(self) -> List[str]:
        """Argv bounding a poll call by what is left of the whole poll's deadline, which _poll_command sets."""
        if not self.timeout or self.timeout <= 0:
            return []
        return ["timeout", "-k", str(KILL_GRACE_SECONDS), f'"$({_POLL_LEFT_FN})"']

    def _poll_plan(self) -> List[Tuple[str, str]]:
        """The named calls this poll makes, in the order they run."""
        calls = [('list', self._list_command())]
        if self.collect_stats:
            calls.append(('info', self._info_command()))
        archive = self._canary_archive()
        if archive and self._canary_due():
            calls += self._canary_calls(archive)
        if self.check_units:
            calls.append(('checks', _check_unit_script(self.check_units)))
        return calls

    def _poll_command(self) -> str:
        """The poll as one command: run as separate commands the calls below would be dispatched concurrently and contend for this repo's chunks cache lock."""
        return self._sequence_command([command for _, command in self._poll_plan()])

    def _sequence_command(self, calls: List[str], limits: Optional[Dict[int, int]] = None,
                          filters: Optional[Dict[int, str]] = None) -> str:
        """Runs calls one after another under one deadline and frames each one's output; `filters` pipes a call's stdout through a shell filter and `limits` then caps it at that many lines."""
        base = f"${_POLL_BASE_DIR_VAR}"
        # The trap is what keeps the throwaway dir throwaway, since borg builds a full chunks cache in it on every poll
        parts = [
            f"{_POLL_BASE_DIR_VAR}=$(mktemp -d)",
            f"trap 'rm -rf \"{base}\"' EXIT",
        ]
        if self.timeout and self.timeout > 0:
            # One deadline for the poll rather than one per call, so that running them in sequence cannot outlast the monitor's interval.
            parts.append(f"{_POLL_DEADLINE_VAR}=$(( $(date +%s) + {int(self.timeout)} ))")
            parts.append(
                f"{_POLL_LEFT_FN}() {{ __vigil_left=$(( {_POLL_DEADLINE_VAR} - $(date +%s) )); "
                f'[ "$__vigil_left" -lt 1 ] && __vigil_left=1; printf %s "$__vigil_left"; }}'
            )
        for index, call in enumerate(calls):
            parts.append(
                f'{call} >"{base}/{index}.out" 2>"{base}/{index}.err"; {_POLL_RC_VAR}=$?'
            )
            for stream in ("out", "err"):
                marker = _frame_line(index, stream, f"${_POLL_RC_VAR}")
                # The closing newline is what keeps output with no trailing one from running into the next marker.
                parts.append(f'printf \'%s\\n\' "{marker}"')
                limit = (limits or {}).get(index) if stream == "out" else None
                keep = (filters or {}).get(index) if stream == "out" else None
                reader = f'{keep} "{base}/{index}.{stream}"' if keep else f'cat "{base}/{index}.{stream}"'
                parts.append(f"{reader} | head -n {int(limit)}" if limit else reader)
                parts.append("printf '\\n'")
        return "; ".join(parts)

    def _list_command(self) -> str:
        return self._build([
            self.borg_bin, "list",
            "--last", str(self.list_archives),
            "--json",
            "--bypass-lock",
            "--lock-wait", str(self.lock_wait),
            self.repo,
        ], persistent_cache=self.cache_dir_configured, bounded=True, wrapped=False)

    def _info_command(self) -> str:
        return self._build([
            self.borg_bin, "info",
            "--json",
            "--last", str(self.list_archives),
            "--bypass-lock",
            "--lock-wait", str(self.lock_wait),
            self.repo,
        ], persistent_cache=self.cache_dir_configured, bounded=True, wrapped=False)

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
        args += self._exclude_args()
        args += ["--lock-wait", str(self.backup_lock_wait)]
        args.append(f"{self.repo}::{name}")
        args += self.source_paths
        return self._build(args, persistent_cache=True)

    def _exclude_args(self) -> List[str]:
        """The exclusion options a backup applies, which a purge also uses to rewrite existing archives to match."""
        args = []
        if self.exclude_caches:
            args.append("--exclude-caches")
        for marker in self.exclude_if_present:
            args += ["--exclude-if-present", marker]
        for pattern in self.exclude:
            args += ["--exclude", pattern]
        if self.exclude_from:
            args += ["--exclude-from", self.exclude_from]
        return args

    def default_archive_name(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return f"{self.archive_prefix}-{stamp}"

    def commands(self) -> List[Command]:
        # While a backup is running, this monitor cycle polls the detached job
        # (a plain SSH command) instead of listing the repo — the same command
        # handler, no streaming channel.
        job = self._running_job()
        # parse() must read the results the way they were requested, even if a job starts or ends mid-cycle.
        self._polling_job = job
        if job is not None:
            return [Command(detached.poll_command(job['workdir'], job['pid'], job['output_seq']))]
        if not self.repo:
            return []
        plan = self._poll_plan()
        self._poll_calls = [name for name, _ in plan]
        return [Command(self._sequence_command([command for _, command in plan]))]

    def _running_job(self) -> Optional[dict]:
        job = self.jobs.running() if self.jobs else None
        if job and job.get('pid') and job.get('workdir'):
            return job
        return None

    def parse(self, results: List[CmdResult]) -> CollectResult:
        job, self._polling_job = self._polling_job, None
        if job is not None:
            return self._parse_poll(job, results[0])

        if not self.repo:
            return CollectResult.failed("No 'repo' configured for borg monitor")

        # One command carried the whole poll, so unpack its transcript before reading the calls back.
        names = self._poll_calls or (['list', 'info'] if self.collect_stats else ['list'])
        by_name = dict(zip(names, _split_poll(results[0], len(names))))
        list_result = by_name['list']
        stdout, stderr, ret = list_result.stdout, list_result.stderr, list_result.exit_code
        logs = [(f"Running: {_redact(self._list_command())}", "INFO")]

        if ret != 0:
            detail = (stderr or stdout).strip()
            logs.append((f"borg list failed (exit {ret}): {detail}", "ERROR"))
            hint = _failure_hint(detail)
            if hint:
                logs.append((hint, "ERROR"))
            unavailable = _list_unavailable(ret, detail)
            return CollectResult(logs=logs, status='unavailable' if unavailable else 'failed')

        view = RepoView.from_stdout(stdout)

        if not view.valid:
            logs.append(("Could not parse borg output — no archive timestamps found", "ERROR"))
            snippet = (stdout or stderr or "").strip()[:500]
            if snippet:
                logs.append((f"Raw output was: {snippet}", "ERROR"))
            return CollectResult(logs=logs, status='unavailable')

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

        if self.collect_stats and 'info' in by_name:
            stats_metrics, stats_metadata, stats_logs, merged_archives = self._parse_repo_stats(
                by_name['info'], view.archives,
            )
            metrics.update(stats_metrics)
            metadata.update(stats_metadata)
            logs.extend(stats_logs)
            if merged_archives is not None:
                metrics['archive_list'] = float(len(merged_archives))
                metadata['archive_list'] = json.dumps({'archives': merged_archives, 'repository': view.info})

        extra = [self._fold_canary(by_name, metrics, metadata, logs),
                 self._fold_checks(by_name, metrics, metadata, logs)]
        if 'failed' in extra:
            status = 'failed'
        return CollectResult(metrics=metrics, metadata=metadata, logs=logs, status=status)

    # --- Restore canary ---------------------------------------------------

    def _canary_archive(self) -> Optional[str]:
        """The newest archive the last poll saw, which the canary is restored from."""
        if not self.canary_path or self.data is None:
            return None
        archives, _ = self.cached_archives()
        return archives[0].get('name') if archives else None

    def _canary_due(self) -> bool:
        checked = self.data.latest_metric('canary_checked_epoch') if self.data is not None else None
        return checked is None or time.time() - checked.value >= self.canary_interval

    def _canary_calls(self, archive: str) -> List[Tuple[str, str]]:
        """Extracts the canary from `archive` to stdout, then reads the live file it should match."""
        extract = self._build([
            self.borg_bin, "extract", "--stdout",
            "--bypass-lock",
            "--lock-wait", str(self.lock_wait),
            f"{self.repo}::{archive}",
            _archive_member(self.canary_path) or self.canary_path,
        ], persistent_cache=self.cache_dir_configured, bounded=True, wrapped=False)
        live = ("sudo -n " if self.require_sudo else "") + "cat " + shlex.quote(self.canary_path)
        return [('canary', extract), ('canary_live', live)]

    def _fold_canary(self, by_name: Dict[str, CmdResult], metrics: Dict[str, float],
                     metadata: Dict[str, str], logs: List[tuple]) -> Optional[str]:
        """Records this poll's restore check if one ran, and returns 'failed' while the last verdict stands failed."""
        if 'canary' in by_name:
            verdict, detail = _canary_verdict(by_name['canary'], by_name.get('canary_live', CmdResult(1, '', _CUT_SHORT)))
            if verdict is None:
                logs.append((f"Restore check skipped: {detail}", "WARNING"))
            else:
                metrics['canary_ok'] = 1.0 if verdict else 0.0
                metrics['canary_checked_epoch'] = float(int(time.time()))
                metadata['canary_ok'] = detail
                logs.append((f"Restore check {'passed' if verdict else 'failed'}: {detail}",
                             "INFO" if verdict else "ERROR"))
                return None if verdict else 'failed'
        if not self.canary_path or self.data is None:
            return None
        last = self.data.latest_metric('canary_ok')
        return 'failed' if last is not None and last.value < 0.5 else None

    # --- Check freshness --------------------------------------------------

    def _stored_checks(self) -> Dict[str, Dict[str, Any]]:
        metric = self.data.latest_metric('check_ok_epoch') if self.data is not None else None
        try:
            data = json.loads(metric.metadata) if metric is not None and metric.metadata else {}
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _fold_checks(self, by_name: Dict[str, CmdResult], metrics: Dict[str, float],
                     metadata: Dict[str, str], logs: List[tuple]) -> Optional[str]:
        """Folds the check units' journal into the stored record and returns 'failed' when any is failed or stale."""
        if 'checks' not in by_name:
            return None
        result = by_name['checks']
        seen = _parse_check_units(result.stdout)
        if not seen:
            logs.append((f"Could not read the check units' journal: {(result.stderr or 'no output').strip()[:200]}", "WARNING"))
            return None
        stored = self._stored_checks()
        states = {unit: _merge_unit(stored.get(unit, {}), _observed_unit(seen.get(unit, {}), self.repo))
                  for unit in self.check_units}

        now = int(time.time())
        problems = []
        for unit, state in states.items():
            if state.get('failed'):
                problems.append(f"{unit} last run failed: {state.get('error') or 'see its journal'}")
            elif not state.get('ok'):
                problems.append(f"{unit} has no successful run on record")
            elif now - state['ok'] > self.check_max_age:
                problems.append(f"{unit} last succeeded {format_age(now - state['ok'])}, "
                                f"exceeds check_max_age of {format_duration(self.check_max_age)}")
        oks = [state.get('ok') or 0 for state in states.values()]
        metrics['check_ok_epoch'] = float(min(oks)) if oks else 0.0
        metrics['checks_ok'] = 0.0 if problems else 1.0
        metadata['check_ok_epoch'] = json.dumps(states)
        for problem in problems:
            logs.append((f"Repository check: {problem}", "ERROR"))
        if not problems:
            logs.append((f"Repository checks passed, oldest {format_age(now - min(oks))}", "INFO"))
        return 'failed' if problems else None

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
        actions = []
        if self.source_paths:
            actions += [
                {'name': 'Run Backup', 'action_id': 'run_backup',
                 'variant': 'primary', 'icon': 'backup'},
                {'name': 'Dry Run', 'action_id': 'dry_run_backup',
                 'variant': 'secondary', 'icon': 'fact_check'},
            ]
        if self.canary_path:
            actions.append({'name': 'Verify Restore', 'action_id': 'verify_restore',
                            'variant': 'secondary', 'icon': 'verified'})
        return actions

    def plan_action(self, action_id: str, **kwargs):
        planners = {
            'run_backup': self._plan_backup,
            'dry_run_backup': self._plan_backup,
            'prune_preview': self._plan_prune_preview,
            'verify_restore': self._plan_verify_restore,
            'browse_archive': self._plan_browse,
            'restore_archive': self._plan_restore,
            'check_repo': self._plan_check,
            'compact_repo': self._plan_compact,
            'prune_repo': self._plan_prune,
            'delete_archive': self._plan_delete,
            'break_lock': self._plan_break_lock,
            'purge_preview': self._plan_purge,
            'purge_excluded': self._plan_purge,
            'diff_archive': self._plan_diff,
        }
        planner = planners.get(action_id)
        return planner(action_id=action_id, **kwargs) if planner else None

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        interpreters = {
            'prune_preview': self._interpret_prune_preview,
            'verify_restore': self._interpret_verify_restore,
            'browse_archive': self._interpret_browse,
            'break_lock': self._interpret_break_lock,
            'diff_archive': self._interpret_diff,
        }
        if action_id in interpreters:
            return interpreters[action_id](result, **kwargs)
        if action_id not in _LAUNCHED:
            return result.exit_code == 0

        kind, redacted, workdir, started = getattr(self, '_pending_launch', None) or ('job', '', '', '')
        self._pending_launch = None

        pid = detached.parse_launch(result.stdout) if result.exit_code == 0 else None
        if pid is None:
            return CollectResult.failed(
                f"Failed to launch {kind}: {(result.stderr or result.stdout).strip()[:200]}")

        job_id = self.jobs.create(kind, redacted, workdir)
        self.jobs.set_pid(job_id, pid)
        started = f"{kind.capitalize()} started (pid {pid}){started}"
        return CollectResult(
            logs=[(started, "INFO")],
            metadata={'content': started},
            success=True,
        )

    @staticmethod
    def _refused(message: str) -> CollectResult:
        """An action outcome that reports a problem without touching the monitor's status."""
        return CollectResult(logs=[(message, "WARNING")], metadata={'content': message}, success=False)

    def _run_one(self, call: str, limit: Optional[int] = None, keep: Optional[str] = None) -> ActionPlan:
        """A single framed borg call as an action, bounded like a poll."""
        return ActionPlan(self._sequence_command([call], limits={0: limit} if limit else None,
                                                 filters={0: keep} if keep else None),
                          timeout=self.timeout or None)

    def _launch(self, kind: str, command: str, started: str = '') -> ActionPlan:
        """Launches `command` as a detached job; interpret_action records the pid it prints and creates the job row."""
        token = f"{self.id}-{int(time.time())}"
        workdir = detached.workdir_for(token)
        self._pending_launch = (kind, _redact(command), workdir, started)
        return ActionPlan(detached.launch_command(command, workdir))

    def _job_refusal(self, what: str) -> Optional[CollectResult]:
        """The refusal for starting a job without a repo or while another one runs, else None."""
        if not self.repo:
            return self._refused(f"Cannot {what}: no 'repo' configured")
        if self._running_job() is not None:
            return self._refused("A job is already running for this monitor")
        return None

    def _plan_backup(self, action_id: str, **_):
        if not self.repo:
            return CollectResult.failed("Cannot back up: no 'repo' configured")
        if not self.source_paths:
            return CollectResult.failed("Cannot back up: no 'source_paths' configured")
        if self._running_job() is not None:
            return CollectResult.failed("A backup is already running for this monitor",
                                        level="WARNING", status=None)
        dry_run = action_id == 'dry_run_backup'
        return self._launch('dry-run' if dry_run else 'backup', self._backup_command(dry_run=dry_run))

    # --- Prune preview ----------------------------------------------------

    def _prune_command(self) -> str:
        args = [
            self.borg_bin, "prune", "--dry-run", "--list",
            "--lock-wait", str(self.lock_wait),
            "--glob-archives", self.prune_match,
        ]
        for key, value in self.retention.items():
            args += ["--" + key.replace('_', '-'), str(value)]
        args.append(self.repo)
        return self._build(args, persistent_cache=self.cache_dir_configured, bounded=True, wrapped=False)

    def _plan_prune_preview(self, **_):
        if not self.repo:
            return self._refused("Cannot preview a prune: no 'repo' configured")
        if not self.retention:
            return self._refused("Cannot preview a prune: no keep_* retention configured")
        return self._run_one(self._prune_command())

    def _interpret_prune_preview(self, result: CmdResult, **_):
        call = _split_poll(result, 1)[0]
        text = "\n".join(part for part in (call.stdout, call.stderr) if part)
        if call.exit_code != 0:
            hint = _failure_hint(text)
            return self._refused(f"borg prune --dry-run failed (exit {call.exit_code}): {text.strip()[:300]}"
                                 + (f"\n{hint}" if hint else ""))
        keep, prune = [], []
        for line in text.splitlines():
            match = _PRUNE_LINE.match(line.strip())
            if not match:
                continue
            if match.group('prune'):
                prune.append(f"  {match.group('name')}  {match.group('date')}")
            else:
                keep.append(f"  {match.group('name')}  {match.group('date')}  ({match.group('rule')})")
        policy = " ".join(f"--{k.replace('_', '-')} {v}" for k, v in self.retention.items())
        lines = [f"Policy: {policy}", f"Archives matching: {self.prune_match}", ""]
        lines.append(f"Would prune {len(prune)} of {len(prune) + len(keep)} archive(s):")
        lines += prune or ["  (none)"]
        lines += ["", f"Keeping {len(keep)}:"] + (keep or ["  (none)"])
        content = "\n".join(lines)
        return CollectResult(
            logs=[(f"Prune preview: would prune {len(prune)}, keep {len(keep)}", "INFO")],
            metadata={'content': content}, success=True,
        )

    # --- Verify restore ---------------------------------------------------

    def _plan_verify_restore(self, **_):
        if not self.canary_path:
            return self._refused("Cannot verify a restore: no 'canary_path' configured")
        archive = self._canary_archive()
        if not archive:
            return self._refused("Cannot verify a restore: no archive has been listed yet")
        return ActionPlan(self._sequence_command([command for _, command in self._canary_calls(archive)]),
                          timeout=self.timeout or None)

    def _interpret_verify_restore(self, result: CmdResult, **_):
        by_name = dict(zip(('canary', 'canary_live'), _split_poll(result, 2)))
        metrics: Dict[str, float] = {}
        metadata: Dict[str, str] = {}
        logs: List[tuple] = []
        self._fold_canary(by_name, metrics, metadata, logs)
        message = logs[-1][0] if logs else "Restore check reached no verdict"
        return CollectResult(metrics=metrics, metadata={**metadata, 'content': message}, logs=logs,
                             success=metrics.get('canary_ok') == 1.0)

    # --- Browse -----------------------------------------------------------

    def listing(self, archive: str, path: str) -> Optional[List[Dict[str, Any]]]:
        """The children of `path` in `archive` as last listed, or None when that folder has not been listed yet."""
        key = (archive, _archive_member(path) or '')
        if key in self._listings:
            self._listings.move_to_end(key)
        return self._listings.get(key)

    def _plan_browse(self, archive: Optional[str] = None, path: Optional[str] = None, **_):
        member = _archive_member(path or '')
        if not self.repo or not archive:
            return self._refused("Cannot browse: no archive given")
        if member is None:
            return self._refused("A path may not contain '.' or '..' components")
        cached = self.listing(archive, member)
        if cached is not None:
            return CollectResult(metadata={'content': self._listing_text(archive, member, cached)}, success=True)
        args = [
            self.borg_bin, "list",
            "--format", _LIST_FORMAT,
            "--bypass-lock",
            "--lock-wait", str(self.lock_wait),
            f"{self.repo}::{archive}",
        ]
        if member:
            args.append(member)
        call = self._build(args, persistent_cache=self.cache_dir_configured, bounded=True, wrapped=False)
        return self._run_one(call, limit=self.browse_limit + 1, keep=_children_filter(member))

    def _interpret_browse(self, result: CmdResult, archive: Optional[str] = None,
                          path: Optional[str] = None, **_):
        call = _split_poll(result, 1)[0]
        if call.exit_code != 0:
            detail = (call.stderr or call.stdout).strip()
            hint = _failure_hint(detail)
            return self._refused(f"borg list failed (exit {call.exit_code}): {detail[:300]}"
                                 + (f"\n{hint}" if hint else ""))
        member = _archive_member(path or '') or ''
        entries = _parse_listing(call.stdout)
        # A listing longer than browse_limit is kept one entry over it, which is how the browser knows it was cut short.
        self._listings[(archive, member)] = entries
        while len(self._listings) > _LISTING_CACHE:
            self._listings.popitem(last=False)
        return CollectResult(metadata={'content': self._listing_text(archive, member, entries)}, success=True)

    def _listing_text(self, archive: str, member: str, entries: List[Dict[str, Any]]) -> str:
        where = f"{archive}:/{member}"
        if not entries:
            return f"{where} — nothing at that path"
        header = f"{where} — {len(entries)} entries"
        if len(entries) > self.browse_limit:
            entries = entries[:self.browse_limit]
            header = f"{where} — first {self.browse_limit} entries (raise browse_limit for more)"
        lines = [f"{e['name']}/" if e['dir'] else f"{e['name']}  {_format_size(e['size'] or 0)}  {e['mtime']}"
                 for e in entries]
        return "\n".join([header, ""] + lines)

    # --- Restore ----------------------------------------------------------

    def _restore_command(self, archive: str, members: List[str], dest: str) -> str:
        """Creates a fresh directory under restore_dir and extracts the paths into it, never over live files."""
        extract = self._build([
            self.borg_bin, "extract",
            "--log-json",
            "--progress",
            "--lock-wait", str(self.backup_lock_wait),
            f"{self.repo}::{archive}",
            *members,
        ], persistent_cache=True)
        mkdir = ("sudo -n " if self.require_sudo else "") + "mkdir -p " + shlex.quote(dest)
        return f"{mkdir} && cd {shlex.quote(dest)} && {extract}"

    def _plan_restore(self, archive: Optional[str] = None, path: Optional[str] = None,
                      paths: Optional[List[str]] = None, **_):
        members = [_archive_member(p) for p in ([path] if path else []) + list(paths or [])]
        if not self.repo or not archive:
            return self._refused("Cannot restore: no archive given")
        if any(m is None for m in members):
            return self._refused("A path may not contain '.' or '..' components")
        if not members or not all(members):
            return self._refused("Give the path to restore — a whole archive is not restored from here")
        refusal = self._job_refusal("restore")
        if refusal:
            return refusal
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = f"{self.restore_dir.rstrip('/')}/{archive}-{stamp}"
        return self._launch('restore', self._restore_command(archive, members, dest), f" into {dest}")

    # --- Maintenance ------------------------------------------------------

    def _maintenance_command(self, args: List[str]) -> str:
        return self._build([self.borg_bin, *args, "--log-json", "--lock-wait", str(self.backup_lock_wait)],
                           persistent_cache=True)

    def _plan_check(self, **_):
        refusal = self._job_refusal("check the repository")
        if refusal:
            return refusal
        args = ["check", "--progress"] + (["--verify-data"] if self.check_verify_data else [])
        return self._launch('check', self._maintenance_command(args + [self.repo]))

    def _plan_compact(self, **_):
        refusal = self._job_refusal("compact the repository")
        if refusal:
            return refusal
        return self._launch('compact', self._maintenance_command(["compact", "--progress", self.repo]))

    def _plan_prune(self, **_):
        if not self.allow_delete:
            return self._refused("Pruning is off for this monitor — set allow_delete: true to enable it")
        if not self.retention:
            return self._refused("Cannot prune: no keep_* retention configured")
        refusal = self._job_refusal("prune")
        if refusal:
            return refusal
        args = ["prune", "--list", "--stats", "--glob-archives", self.prune_match]
        for key, value in self.retention.items():
            args += ["--" + key.replace('_', '-'), str(value)]
        return self._launch('prune', self._maintenance_command(args + [self.repo]))

    def _plan_delete(self, archive: Optional[str] = None, **_):
        if not self.allow_delete:
            return self._refused("Deleting archives is off for this monitor — set allow_delete: true to enable it")
        archives, _ = self.cached_archives()
        # Only an archive the monitor has listed may be deleted, so a stale or hand-typed name cannot reach borg.
        if not archive or archive not in {a.get('name') for a in archives}:
            return self._refused(f"Cannot delete {archive or 'an unnamed archive'}: it is not in the archive list")
        refusal = self._job_refusal("delete an archive")
        if refusal:
            return refusal
        self._listings = OrderedDict((k, v) for k, v in self._listings.items() if k[0] != archive)
        return self._launch('delete', self._maintenance_command(["delete", "--stats", f"{self.repo}::{archive}"]),
                            f" for {archive}")

    def _purge_refusal(self) -> Optional[CollectResult]:
        """The refusal for a purge this monitor is not set up for, else None."""
        if not self.allow_purge:
            return self._refused("Purging is off for this monitor — set allow_purge: true to enable it")
        if not (self.exclude or self.exclude_from or self.exclude_if_present or self.exclude_caches):
            return self._refused("Cannot purge: no exclude patterns configured")
        return self._job_refusal("purge excluded files")

    def _plan_purge(self, action_id: str, **_):
        refusal = self._purge_refusal()
        if refusal:
            return refusal
        scope = ["--glob-archives", self.purge_match] if self.purge_match else []
        if action_id == 'purge_preview':
            recreate = self._build([self.borg_bin, "recreate", "--dry-run", "--list", "--filter=x", *scope,
                                    *self._exclude_args(), "--lock-wait", str(self.backup_lock_wait), self.repo],
                                   persistent_cache=True)
            command = f'{{ {recreate} 2>&1; echo "__vigil_rc=$?"; }} | {_TOPMOST_EXCLUDED}'
            return self._launch('purge-preview', command)
        # An interrupted recreate leaves a <name>.recreate archive behind that makes the next attempt fail at once.
        leftovers = self._maintenance_command(["delete", "--glob-archives", "*.recreate", self.repo])
        recreate = self._maintenance_command(["recreate", "--info", "--stats", "--progress", *scope,
                                              *self._exclude_args(), self.repo])
        compact = self._maintenance_command(["compact", "--progress", self.repo])
        # A plain `exit` would leave the job's shell before it records the status, so a subshell carries the failure instead.
        command = f'{leftovers}; {recreate}; rc=$?; if [ "$rc" -le 1 ]; then {compact}; else (exit "$rc"); fi'
        return self._launch('purge', command)

    def _plan_break_lock(self, **_):
        if not self.allow_delete:
            return self._refused("Breaking locks is off for this monitor — set allow_delete: true to enable it")
        refusal = self._job_refusal("break the lock")
        if refusal:
            return refusal
        return self._run_one(self._build([self.borg_bin, "break-lock", self.repo],
                                         persistent_cache=True, bounded=True, wrapped=False))

    def _interpret_break_lock(self, result: CmdResult, **_):
        call = _split_poll(result, 1)[0]
        if call.exit_code != 0:
            detail = (call.stderr or call.stdout).strip()
            return self._refused(f"borg break-lock failed (exit {call.exit_code}): {detail[:300]}")
        message = "Broke the repository and cache locks"
        return CollectResult(logs=[(message, "WARNING")], metadata={'content': message}, success=True)

    # --- Diff -------------------------------------------------------------

    def _previous_archive(self, archive: str) -> Optional[str]:
        """The listed archive just older than `archive`, which a diff compares it against."""
        names = [a.get('name') for a in self.cached_archives()[0]]
        if archive not in names:
            return None
        index = names.index(archive)
        return names[index + 1] if index + 1 < len(names) else None

    def _plan_diff(self, archive: Optional[str] = None, **_):
        if not self.repo or not archive:
            return self._refused("Cannot diff: no archive given")
        older = self._previous_archive(archive)
        if older is None:
            return self._refused(f"{archive} is the oldest listed archive — there is nothing before it to compare")
        call = self._build([
            self.borg_bin, "diff",
            "--bypass-lock",
            "--lock-wait", str(self.lock_wait),
            f"{self.repo}::{older}", archive,
        ], persistent_cache=self.cache_dir_configured, bounded=True, wrapped=False)
        return self._run_one(call, limit=self.browse_limit + 1)

    def _interpret_diff(self, result: CmdResult, archive: Optional[str] = None, **_):
        call = _split_poll(result, 1)[0]
        if call.exit_code != 0:
            detail = (call.stderr or call.stdout).strip()
            hint = _failure_hint(detail)
            return self._refused(f"borg diff failed (exit {call.exit_code}): {detail[:300]}"
                                 + (f"\n{hint}" if hint else ""))
        lines = [line for line in call.stdout.splitlines() if line.strip()]
        header = f"Changes from {self._previous_archive(archive)} to {archive}: {len(lines)}"
        if len(lines) > self.browse_limit:
            lines = lines[:self.browse_limit]
            header = f"{header.rsplit(':', 1)[0]}: first {self.browse_limit} (raise browse_limit for more)"
        return CollectResult(metadata={'content': "\n".join([header, ""] + (lines or ["(no changes)"]))},
                             success=True)

    def _parse_poll(self, job: dict, result: CmdResult) -> CollectResult:
        """Advance a running detached backup from one poll's output. Appends
        new output lines, updates the progress summary, and on completion
        finalizes the Job row and returns the interpreted outcome."""
        job_id = job['id']
        # A poll that never reached the target says nothing about the job; only a real answer may finish it.
        if result.exit_code != 0 and not result.stdout.strip():
            return CollectResult(logs=[(
                f"Could not poll the running {job['kind']}: "
                f"{(result.stderr or '').strip()[:200] or f'exit {result.exit_code}'}", 'WARNING')])
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
            # recreate prints which archive it is on as plain text, even under --log-json.
            if text.startswith('Processing '):
                summary = text.strip()
                continue
            if not text.startswith('{'):
                continue
            try:
                record = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(record, dict) and record.get('type') == 'progress_message':
                if record.get('message') and not record.get('finished'):
                    summary = record['message'].strip()
                continue
            if isinstance(record, dict) and record.get('type') == 'progress_percent':
                if record.get('message') and not record.get('finished'):
                    summary = record['message'].strip()
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
            return 'UNKNOWN', 'unavailable'
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

    def _canary_pair(self) -> (str, Optional[str]):
        """Returns the (text, color) pair for the restore-check card."""
        if not self.canary_path:
            return 'Not configured', None
        ok = self.data.latest_metric('canary_ok')
        checked = self.data.latest_metric('canary_checked_epoch')
        if ok is None or checked is None:
            return 'Pending', 'unavailable'
        when = format_age(int(time.time()) - int(checked.value))
        return (f'Passed {when}', 'online') if ok.value >= 0.5 else (f'Failed {when}', 'failed')

    def _checks_pair(self) -> (str, Optional[str]):
        """Returns the (text, color) pair for the repository-checks card."""
        if not self.check_units:
            return 'Not configured', None
        ok = self.data.latest_metric('checks_ok')
        oldest = self.data.latest_metric('check_ok_epoch')
        if ok is None or oldest is None:
            return 'Pending', 'unavailable'
        if ok.value < 0.5:
            return 'Failed' if any(s.get('failed') for s in self._stored_checks().values()) else 'Stale', 'failed'
        return f'Passed {format_age(int(time.time()) - int(oldest.value))}', 'online'

    @property
    def _canary_text(self) -> str:
        return self._canary_pair()[0]

    @property
    def _canary_color(self) -> Optional[str]:
        return self._canary_pair()[1]

    @property
    def _checks_text(self) -> str:
        return self._checks_pair()[0]

    @property
    def _checks_color(self) -> Optional[str]:
        return self._checks_pair()[1]

    def backup_summary(self) -> Dict[str, Any]:
        """This repository's backup health in one flat record, for the /api/backups roll-up."""
        now = int(time.time())
        epoch = self._epoch()
        newest = int(epoch) if epoch else None
        canary_ok = self.data.latest_metric('canary_ok') if self.canary_path else None
        canary_at = self.data.latest_metric('canary_checked_epoch') if self.canary_path else None
        checks_ok = self.data.latest_metric('checks_ok') if self.check_units else None
        check_at = self.data.latest_metric('check_ok_epoch') if self.check_units else None
        return {
            'repo': self.repo,
            'newest_archive_epoch': newest,
            'newest_archive_age': now - newest if newest else None,
            'max_age': self.max_age,
            'fresh': bool(newest) and now - newest <= self.max_age,
            'restore_check': None if canary_ok is None else canary_ok.value >= 0.5,
            'restore_checked_epoch': int(canary_at.value) if canary_at else None,
            'repo_checks': None if checks_ok is None else checks_ok.value >= 0.5,
            'repo_checked_epoch': int(check_at.value) if check_at and check_at.value else None,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.plugins.borg.browser import render_page
        render_page(self, context)

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
                'canary_card': {'title': 'RESTORE CHECK', 'value_attr': '_canary_text',
                                'color_attr': '_canary_color'},
                'checks_card': {'title': 'REPO CHECKS', 'value_attr': '_checks_text',
                                'color_attr': '_checks_color'},
            },
            'buttons': {
                'tools': [
                    {'id': 'prune_preview', 'label': 'Prune Preview', 'icon': 'content_cut',
                     'kind': 'dialog', 'dialog': 'prune_preview', 'visible_if': lambda p: bool(p.retention)},
                    {'id': 'verify_restore', 'label': 'Verify Restore', 'icon': 'verified',
                     'visible_if': lambda p: bool(p.canary_path)},
                    {'id': 'check_repo', 'label': 'Check', 'icon': 'health_and_safety',
                     'visible_if': lambda p: bool(p.repo)},
                    {'id': 'compact_repo', 'label': 'Compact', 'icon': 'compress',
                     'visible_if': lambda p: bool(p.repo)},
                    {'id': 'prune_repo', 'label': 'Prune', 'icon': 'delete_sweep', 'color': 'negative',
                     'visible_if': lambda p: p.allow_delete and bool(p.retention),
                     'confirm': 'Prune {plugin.repo} by its retention policy? Archives it drops are gone for good '
                                '— run Prune Preview first to see which. Space is freed by a Compact afterwards.'},
                    {'id': 'purge_preview', 'label': 'Purge Preview', 'icon': 'filter_alt',
                     'visible_if': lambda p: p.allow_purge},
                    {'id': 'purge_excluded', 'label': 'Purge Excluded', 'icon': 'cleaning_services', 'color': 'negative',
                     'visible_if': lambda p: p.allow_purge,
                     'confirm': 'Rewrite every archive in {plugin.repo} without the files its exclude patterns now drop, '
                                'then compact? Those files are gone from every archive for good — run Purge Preview first '
                                'to see which. It holds the repository lock for the whole run, so backups wait until it ends.'},
                    {'id': 'break_lock', 'label': 'Break Lock', 'icon': 'lock_open', 'color': 'negative',
                     'visible_if': lambda p: p.allow_delete,
                     'confirm': 'Break the locks on {plugin.repo}? Only do this when no borg process is using it; '
                                'breaking a live lock can corrupt the repository.'},
                ],
            },
            'dialogs': {
                'prune_preview': {'kind': 'read', 'title': 'Prune Preview: {plugin.name}',
                                  'action_id': 'prune_preview', 'render': 'textarea_readonly'},
                'diff': {'kind': 'read', 'title': 'Changes in {row[name]}', 'action_id': 'diff_archive',
                         'params': {'archive': 'name'}, 'render': 'textarea_readonly'},
                'restore': {'kind': 'form', 'title': 'Restore from {row[name]}', 'action_id': 'restore_archive',
                            'params': {'archive': 'name'}, 'submit_label': 'Restore',
                            'fields': [{'name': 'path', 'label': 'Path to restore',
                                        'placeholder': f'Extracted into a new folder under {self.restore_dir}'}]},
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
                    'row_actions': [
                        {'id': 'diff', 'icon': 'difference', 'tooltip': 'Changes since the previous archive',
                         'kind': 'dialog', 'dialog': 'diff'},
                        {'id': 'restore', 'icon': 'restore', 'tooltip': 'Restore a path', 'kind': 'dialog', 'dialog': 'restore'},
                        {'id': 'delete', 'icon': 'delete', 'tooltip': 'Delete archive', 'color': 'negative',
                         'action_id': 'delete_archive', 'params': {'archive': 'name'},
                         'visible_if': lambda p: p.allow_delete,
                         'confirm': 'Delete archive {row[name]}? It cannot be recovered.'},
                    ],
                },
            },
            'job_panel': {
                'widget': 'jobs',
                'title': 'JOBS',
                'run_action_id': 'run_backup', 'run_label': 'Run Backup', 'run_icon': 'play_arrow',
                'cancel_label': 'Cancel', 'cancel_icon': 'stop',
                'enabled_if': lambda p: bool(p.source_paths),
                'disabled_text': 'Run Backup needs source_paths — the other jobs still run from here',
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
