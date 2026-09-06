"""Nix garbage collection: whether the store is actually being reclaimed on
this host. One cheap script per cycle reads the store filesystem and path
count, every profile's generation links, the `nix-gc` unit and timer state,
and the last collection's own summary line from the journal — so a collection
that stopped running, failed, or was never scheduled reads as a failure rather
than as a slowly filling disk. The last run's epoch and yield are carried
forward as metric metadata, so a journal that has since rotated past it does
not erase what was already seen. The action launches a detached
`nix-collect-garbage` on the target, the same collection the timer runs.
Config: unit, timer, store, max_age, stale_status, timer_status, warning,
threshold, max_generations, max_generation_age, require_sudo, gc_bin,
gc_args."""

import json
import re
import shlex
import time
from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors import ssh_connector as detached
from vigil.core.connectors.types import (
    ActionPlan, CmdResult, Command, CollectResult, Status,
)
from vigil.plugins.base.plugin_helpers import (
    StatusAccumulator, format_age, format_bytes, format_duration, parse_duration,
)


_DEFAULT_LAYOUT = [
    ['host_card', 'last_gc_card', 'freed_card', 'next_gc_card'],
    ['store_card', 'paths_card', 'generations_card', 'roots_card'],
    ['profiles'],
    ['chart'],
    ['jobs'],
    ['events'],
]

# Metric carrying the last collection's epoch as its value and the rest of what
# was seen of that run as JSON metadata, so a rotated journal loses nothing.
_STATE_METRIC = 'last_gc_epoch'

_SUMMARY = re.compile(r'(\d+)\s+store paths deleted,\s+([\d.]+)\s*(\w+)\s+freed')

# systemd logs exactly one of these when a run ends, so the journal — not the unit's
# Result, which a daemon re-exec resets to success — is what decides the outcome.
_FAILED, _FINISHED = 'Failed with result', 'Finished '

# How far from a run's outcome line its summary and error lines may be and still be
# that run's. They land within seconds of each other; this only rejects a stale line
# the journal has kept from an earlier collection.
_RUN_WINDOW = 300

# The units nix renders a collection's yield in, as bytes.
_SIZE_UNITS = {
    'b': 1, 'byte': 1, 'bytes': 1,
    'kb': 1000 ** 1, 'mb': 1000 ** 2, 'gb': 1000 ** 3, 'tb': 1000 ** 4,
    'kib': 1024 ** 1, 'mib': 1024 ** 2, 'gib': 1024 ** 3, 'tib': 1024 ** 4,
    'pib': 1024 ** 5,
}


def _probe_script(unit: str, timer: str, store: str) -> str:
    """One script collecting every cheap fact about collection on this host."""
    unit, timer, store = shlex.quote(unit), shlex.quote(timer), shlex.quote(store)
    return '\n'.join([
        'echo "hostname=$(uname -n)"',
        f'echo "df=$(df -B1 --output=size,used,avail,target {store} 2>/dev/null | tail -1)"',
        f'echo "paths=$(ls -U {store} 2>/dev/null | wc -l)"',
        'echo "roots=$(ls -U /nix/var/nix/gcroots/auto 2>/dev/null | wc -l)"',
        f'echo "svc_state=$(systemctl show {unit} -p ActiveState --value 2>/dev/null)"',
        f'echo "svc_result=$(systemctl show {unit} -p Result --value 2>/dev/null)"',
        f'echo "svc_exit=$(systemctl show {unit} --timestamp=unix -p InactiveEnterTimestamp --value 2>/dev/null)"',
        f'echo "timer_load=$(systemctl show {timer} -p LoadState --value 2>/dev/null)"',
        f'echo "timer_state=$(systemctl show {timer} -p ActiveState --value 2>/dev/null)"',
        f'echo "timer_next=$(systemctl show {timer} --timestamp=unix -p NextElapseUSecRealtime --value 2>/dev/null)"',
        f"echo \"summary=$(journalctl -u {unit} -o short-unix --no-pager -n 1 -g 'store paths deleted' 2>/dev/null | tail -1)\"",
        f"echo \"outcome=$(journalctl -u {unit} -o short-unix --no-pager -n 1 -g 'Finished |Failed with result' 2>/dev/null | tail -1)\"",
        f"echo \"error=$(journalctl -u {unit} -o short-unix --no-pager -n 1 -g 'error:' 2>/dev/null | tail -1)\"",
        # A profile is a symlink whose target is its own generation link; that rules out
        # /nix/var/nix/profiles/default, which points at another profile and would double count.
        'for prof in /nix/var/nix/profiles/* /nix/var/nix/profiles/per-user/*/*; do',
        '  [ -L "$prof" ] || continue',
        '  target=$(readlink "$prof"); base=${prof##*/}',
        '  case "$target" in "$base"-*-link) ;; *) continue ;; esac',
        '  count=0; oldest=',
        '  for link in "$prof"-*-link; do',
        '    [ -L "$link" ] || continue',
        '    count=$((count + 1))',
        '    stamp=$(stat -c %Y "$link" 2>/dev/null)',
        '    if [ -n "$stamp" ] && { [ -z "$oldest" ] || [ "$stamp" -lt "$oldest" ]; }; then oldest=$stamp; fi',
        '  done',
        '  generation=${target#"$base"-}',
        '  echo "profile=$prof|${generation%-link}|$count|$oldest"',
        'done',
    ])


def _parse_probe(stdout: str) -> Dict[str, Any]:
    """Turn the probe's key=value lines into a dict, collecting the repeated
    profile lines into a list."""
    fields: Dict[str, Any] = {'profile': []}
    for line in stdout.splitlines():
        key, sep, value = line.partition('=')
        if not sep:
            continue
        value = value.strip()
        if key == 'profile':
            fields['profile'].append(value)
        else:
            fields[key] = value
    return fields


def _epoch(value: Optional[str]) -> Optional[int]:
    """Read a systemd `--timestamp=unix` value ('@1788757200') as an epoch."""
    if not value:
        return None
    digits = value.lstrip('@').split('.')[0]
    return int(digits) if digits.isdigit() else None


def _log_epoch(line: str) -> Optional[int]:
    """Read the epoch off a journalctl `-o short-unix` line."""
    stamp = line.split(' ', 1)[0] if line else ''
    try:
        return int(float(stamp))
    except ValueError:
        return None


def _log_message(line: str) -> str:
    """Strip the timestamp, host and unit prefix off a journalctl line."""
    _, _, message = line.partition(': ')
    return (message or line).strip()[:200]


def _same_run(epoch: Optional[int], run_epoch: int) -> bool:
    """Whether a journal line close to the run's own end belongs to that run."""
    return epoch is not None and abs(run_epoch - epoch) <= _RUN_WINDOW


def _bytes_from(amount: str, unit: str) -> Optional[float]:
    """Turn nix's rendered '44.4 GiB' yield into bytes."""
    factor = _SIZE_UNITS.get(unit.lower())
    if factor is None:
        return None
    try:
        return float(amount) * factor
    except ValueError:
        return None


def _severity(value, default: str) -> str:
    """Read a configured status name, falling back on anything unrecognised."""
    try:
        return str(Status(str(value)))
    except ValueError:
        return default


def _profile_row(raw: str) -> Optional[Dict[str, Any]]:
    """Turn one `path|generation|count|oldest` probe line into a row."""
    parts = raw.split('|')
    if len(parts) != 4:
        return None
    path, generation, count, oldest = parts
    if not path:
        return None
    return {
        'path': path,
        'generation': int(generation) if generation.isdigit() else None,
        'count': int(count) if count.isdigit() else 0,
        'oldest_epoch': int(oldest) if oldest.isdigit() else None,
    }


class NixGc(Plugin):
    def __init__(self, name: str, config):
        super().__init__(name, config)
        self.unit = str(config.get('unit', 'nix-gc.service'))
        self.timer = str(config.get('timer', 'nix-gc.timer'))
        self.store = str(config.get('store', '/nix/store'))
        self.max_age = parse_duration(config.get('max_age', '2w'))
        self.stale_status = _severity(config.get('stale_status'), 'failed')
        self.timer_status = _severity(config.get('timer_status'), 'warning')
        self.warning = float(config.get('warning', 80))
        self.threshold = float(config.get('threshold', 90))
        self.max_generations = (
            int(config['max_generations']) if config.get('max_generations') is not None else None
        )
        self.max_generation_age = (
            parse_duration(config['max_generation_age'])
            if config.get('max_generation_age') is not None else None
        )
        self.require_sudo = bool(config.get('require_sudo', True))
        self.gc_bin = str(config.get('gc_bin', 'nix-collect-garbage'))
        self.gc_args = list(config.get('gc_args', ['--delete-older-than', '7d']))
        self._pending_launch = None
        self._polling_job = None

        from vigil.core.ui.spec import register_color_rule, threshold_color
        self._store_color = f'nix_gc_store_{self.id}'
        register_color_rule(self._store_color)(
            threshold_color(warning=self.warning, threshold=self.threshold))

    # --- collection ---

    def commands(self) -> List[Command]:
        job = self._running_job()
        # parse() must read the results the way they were requested, even if a job starts or ends mid-cycle.
        self._polling_job = job
        if job is not None:
            return [Command(detached.poll_command(job['workdir'], job['pid'], job['output_seq']))]
        return [Command(_probe_script(self.unit, self.timer, self.store))]

    def _running_job(self) -> Optional[dict]:
        job = self.jobs.running() if self.jobs else None
        if job and job.get('pid') and job.get('workdir'):
            return job
        return None

    def _state(self) -> Dict[str, Any]:
        """The last collection Vigil has seen: its epoch, outcome and yield,
        carried between cycles as metric metadata."""
        metric = self.data.latest_metric(_STATE_METRIC) if self.data else None
        if metric is None or not metric.metadata:
            return {}
        try:
            data = json.loads(metric.metadata)
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def parse(self, results: List[CmdResult]) -> CollectResult:
        job, self._polling_job = self._polling_job, None
        if job is not None:
            return self._parse_poll(job, results[0])

        if not results:
            return CollectResult.failed('No probe result for this cycle', status='offline')

        probe = results[0]
        if probe.exit_code != 0 and not probe.stdout.strip():
            return CollectResult.failed(
                f"Could not read the Nix store: {(probe.stderr or probe.stdout).strip()[:200]}",
                status='offline')

        fields = _parse_probe(probe.stdout)
        if not fields.get('df'):
            return CollectResult.failed(
                f"{self.store} is unreadable — is Nix installed on this host?", status='offline')
        return self._assemble(fields, self._collection_state(fields))

    def _collection_state(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Fold what this cycle saw of the last collection into the stored
        state, keeping the older record whenever the journal no longer has it."""
        seen = self._observed_run(fields)
        stored = self._state()
        if seen is None:
            return stored
        if seen['epoch'] <= int(stored.get('epoch') or 0):
            # The same run seen again; a later cycle may have picked up lines the first one missed.
            return {**stored, **{k: v for k, v in seen.items() if v is not None}}
        return seen

    def _observed_run(self, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """This cycle's reading of the last collection, as one whole record, or
        None when nothing on the host records that a collection ever ran."""
        outcome, summary, error = (str(fields.get(key, ''))
                                   for key in ('outcome', 'summary', 'error'))
        outcome_epoch = _log_epoch(outcome)
        match = _SUMMARY.search(summary)
        summary_epoch = _log_epoch(summary) if match else None

        # The unit's own stop bounds the run when the journal has rotated past every line of it.
        epoch = max((e for e in (outcome_epoch, summary_epoch, _epoch(fields.get('svc_exit')))
                     if e is not None), default=None)
        if epoch is None:
            return None

        run: Dict[str, Any] = {'epoch': epoch, 'deleted': None, 'freed_bytes': None,
                               'error': None}
        # A `Result` of success proves nothing: systemd resets it on a daemon re-exec, which is
        # what a NixOS switch does, so only the journal may call a run failed.
        if _FAILED in outcome:
            run['succeeded'] = False
        elif _FINISHED in outcome:
            run['succeeded'] = True
        else:
            run['succeeded'] = fields.get('svc_result') in (None, '', 'success')

        if match and _same_run(summary_epoch, epoch):
            run['deleted'] = int(match.group(1))
            run['freed_bytes'] = _bytes_from(match.group(2), match.group(3))
        if not run['succeeded'] and _same_run(_log_epoch(error), epoch):
            run['error'] = _log_message(error)
        return run

    def _assemble(self, fields: Dict[str, Any], state: Dict[str, Any]) -> CollectResult:
        """Turn the probe's facts and the stored collection record into this
        cycle's metrics, log lines and worst-of status."""
        acc = StatusAccumulator()
        now = int(time.time())
        metrics: Dict[str, float] = {}
        logs: List[Tuple[str, str]] = []

        metrics.update(self._store_metrics(fields, acc, logs))
        profiles = [row for row in (_profile_row(raw) for raw in fields['profile']) if row]
        metrics.update(self._profile_metrics(profiles, now, acc, logs))
        metrics.update(self._collection_metrics(fields, state, now, acc, logs))

        metrics[_STATE_METRIC] = float(state.get('epoch') or 0)
        return CollectResult(
            metrics=metrics, metadata={_STATE_METRIC: json.dumps(state)},
            logs=logs, status=str(acc.status),
            snapshot={'hostname': fields.get('hostname'), 'profiles': profiles,
                      'timer_load': fields.get('timer_load'),
                      'timer_state': fields.get('timer_state'),
                      'svc_state': fields.get('svc_state'),
                      'svc_result': fields.get('svc_result')},
        )

    def _store_metrics(self, fields: Dict[str, Any], acc: StatusAccumulator,
                       logs: List[Tuple[str, str]]) -> Dict[str, float]:
        """The store's filesystem usage, path count and auto GC root count."""
        metrics: Dict[str, float] = {}
        parts = str(fields.get('df', '')).split()
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
            size, used, avail = (int(p) for p in parts[:3])
            used_pct = (used / size * 100) if size else 0.0
            metrics['store_used_pct'] = used_pct
            metrics['store_size_gb'] = size / (1024 ** 3)
            metrics['store_used_gb'] = used / (1024 ** 3)
            metrics['store_avail_gb'] = avail / (1024 ** 3)
            if used_pct >= self.threshold:
                acc.escalate('failed')
            elif used_pct >= self.warning:
                acc.escalate('warning')
            logs.append((
                f"{self.store} is {used_pct:.1f}% full "
                f"({format_bytes(used / 1024 ** 3)} of {format_bytes(size / 1024 ** 3)}, "
                f"{format_bytes(avail / 1024 ** 3)} free)",
                'WARNING' if used_pct >= self.warning else 'INFO'))

        for key, metric in (('paths', 'store_paths'), ('roots', 'gc_roots')):
            value = str(fields.get(key, ''))
            if value.isdigit():
                metrics[metric] = float(value)
        if 'store_paths' in metrics:
            logs.append((f"{int(metrics['store_paths']):,} store paths, "
                         f"{int(metrics.get('gc_roots', 0)):,} indirect GC roots", 'INFO'))
        return metrics

    def _profile_metrics(self, profiles: List[Dict[str, Any]], now: int,
                         acc: StatusAccumulator,
                         logs: List[Tuple[str, str]]) -> Dict[str, float]:
        """Generation counts and the oldest generation still on disk — what a
        collection with `--delete-older-than` is supposed to be bounding."""
        if not profiles:
            return {}
        metrics = {'profiles': float(len(profiles)),
                   'generations': float(sum(row['count'] for row in profiles))}

        stamps = [row['oldest_epoch'] for row in profiles if row['oldest_epoch']]
        if stamps:
            metrics['oldest_generation_epoch'] = float(min(stamps))
            age = now - min(stamps)
            stale = self.max_generation_age is not None and age > self.max_generation_age
            if stale:
                acc.escalate('warning')
            logs.append((
                f"{int(metrics['generations'])} generations across {len(profiles)} profiles, "
                f"oldest kept {format_age(age)}" + (
                    f", over the {format_duration(self.max_generation_age)} limit" if stale else ''),
                'WARNING' if stale else 'INFO'))

        crowded = [row for row in profiles
                   if self.max_generations is not None and row['count'] > self.max_generations]
        if crowded:
            acc.escalate('warning')
            logs.append((
                'Over the ' + str(self.max_generations) + '-generation limit: ' + ', '.join(
                    f"{row['path']} ({row['count']})" for row in crowded), 'WARNING'))
        return metrics

    def _collection_metrics(self, fields: Dict[str, Any], state: Dict[str, Any], now: int,
                            acc: StatusAccumulator,
                            logs: List[Tuple[str, str]]) -> Dict[str, float]:
        """When collection last ran, what it yielded, and whether it is still
        scheduled to run again."""
        metrics: Dict[str, float] = {}
        running = fields.get('svc_state') in ('active', 'activating', 'reloading')
        if running:
            logs.append((f"{self.unit} is collecting now", 'INFO'))

        if not state.get('succeeded', True):
            acc.escalate('failed')
            reason = state.get('error')
            logs.append((f"The last collection failed" + (f": {reason}" if reason else ''),
                         'ERROR'))
        metrics['last_gc_success'] = 1.0 if state.get('succeeded', True) else 0.0

        epoch = state.get('epoch')
        if not epoch:
            acc.escalate('offline')
            logs.append((f"Could not tell when {self.unit} last ran — no record in its journal "
                         'and no timestamp on the unit', 'WARNING'))
        else:
            age = now - int(epoch)
            stale = age > self.max_age and not running
            if stale:
                acc.escalate(self.stale_status)
            yielded = state.get('freed_bytes')
            detail = (f", freeing {format_bytes(yielded / 1024 ** 3)} "
                      f"across {state.get('deleted') or 0:,} paths" if yielded is not None else '')
            logs.append((
                f"Last collected {format_age(age)}{detail}" + (
                    f", over the {format_duration(self.max_age)} limit" if stale else ''),
                Status(self.stale_status).log_level if stale else 'INFO'))
            if yielded is not None:
                metrics['last_gc_freed_gb'] = yielded / (1024 ** 3)
                metrics['last_gc_deleted'] = float(state.get('deleted') or 0)

        scheduled = fields.get('timer_state') == 'active'
        metrics['timer_active'] = 1.0 if scheduled else 0.0
        if not scheduled:
            acc.escalate(self.timer_status)
            missing = fields.get('timer_load') != 'loaded'
            logs.append((
                f"{self.timer} is " + ('not installed' if missing else 'inactive')
                + ' — nothing collects on a schedule here',
                Status(self.timer_status).log_level))
        next_run = _epoch(fields.get('timer_next'))
        if next_run:
            metrics['next_gc_epoch'] = float(next_run)
            logs.append((f"Next collection in {format_duration(max(next_run - now, 0))}", 'INFO'))
        return metrics

    # --- job polling ---

    def _parse_poll(self, job: dict, result: CmdResult) -> CollectResult:
        """Advance a running detached collection from one poll's output,
        finalizing the Job row when it completes."""
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
            summary = next((line.strip() for line in reversed(lines) if line.strip()), None)
            if summary:
                self.jobs.set_progress(job_id, summary[:200])

        if poll.exit_code is None and poll.alive:
            return CollectResult()

        if poll.exit_code is None:
            self.jobs.finish(job_id, 'failed', exit_code=-1,
                             error='Process ended without writing an exit code')
            return CollectResult.failed('Collection ended unexpectedly')

        state = 'succeeded' if poll.exit_code == 0 else 'failed'
        self.jobs.finish(job_id, state, exit_code=poll.exit_code,
                         error=None if state == 'succeeded' else f"Exited with status {poll.exit_code}")
        if poll.exit_code == 0:
            return CollectResult(logs=[('Collection completed successfully', 'INFO')], success=True)
        return CollectResult.failed(f"Collection failed (exit {poll.exit_code})")

    # --- actions ---

    def get_actions(self) -> List[Dict[str, str]]:
        return [{'name': 'Collect Garbage', 'action_id': 'collect',
                 'variant': 'primary', 'icon': 'delete_sweep'}]

    def _collect_command(self) -> str:
        sudo = 'sudo -n ' if self.require_sudo else ''
        args = ' '.join(shlex.quote(a) for a in self.gc_args)
        return f'{sudo}{self.gc_bin} {args}'.strip()

    def plan_action(self, action_id: str, **kwargs):
        if action_id != 'collect':
            return None
        if self._running_job() is not None:
            return CollectResult.failed('A collection is already running for this monitor',
                                        level='WARNING', status=None)
        # The on-target workdir is named before the Job row exists;
        # interpret_action records the pid the launch prints and creates the row.
        workdir = detached.workdir_for(f'{self.id}-{int(time.time())}')
        self._pending_launch = (self._collect_command(), workdir)
        return ActionPlan(detached.launch_command(self._pending_launch[0], workdir))

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        if action_id != 'collect':
            return result.exit_code == 0

        command, workdir = self._pending_launch or ('', '')
        self._pending_launch = None

        pid = detached.parse_launch(result.stdout) if result.exit_code == 0 else None
        if pid is None:
            return CollectResult.failed(
                f"Failed to launch the collection: {(result.stderr or result.stdout).strip()[:200]}")

        job_id = self.jobs.create('collection', command, workdir)
        self.jobs.set_pid(job_id, pid)
        return CollectResult(logs=[(f"Collection started (pid {pid})", 'INFO')], success=True)

    # --- UI ---

    def _metric(self, name: str) -> Optional[float]:
        metric = self.data.latest_metric(name)
        return metric.value if metric is not None else None

    def _last_gc_pair(self) -> Tuple[str, Optional[str]]:
        state = self._state()
        epoch = state.get('epoch')
        if not epoch:
            return 'UNKNOWN', 'offline'
        age = int(time.time()) - int(epoch)
        if not state.get('succeeded', True):
            return format_age(age), 'failed'
        return format_age(age), self.stale_status if age > self.max_age else 'online'

    def _next_gc_pair(self) -> Tuple[str, Optional[str]]:
        if (self._metric('timer_active') or 0) < 0.5:
            return 'NOT SCHEDULED', self.timer_status
        epoch = self._metric('next_gc_epoch')
        if not epoch:
            return '--', None
        return format_duration(max(int(epoch) - int(time.time()), 0)), 'online'

    @property
    def _last_gc_text(self) -> str:
        return self._last_gc_pair()[0]

    @property
    def _last_gc_color(self) -> Optional[str]:
        return self._last_gc_pair()[1]

    @property
    def _next_gc_text(self) -> str:
        return self._next_gc_pair()[0]

    @property
    def _next_gc_color(self) -> Optional[str]:
        return self._next_gc_pair()[1]

    @property
    def _freed_text(self) -> str:
        state = self._state()
        freed = state.get('freed_bytes')
        if freed is None:
            return '--'
        return f"{format_bytes(freed / 1024 ** 3)} · {state.get('deleted') or 0:,} paths"

    @property
    def _profile_rows(self) -> List[Dict[str, str]]:
        """One row per profile: where it is, which generation is live, how many
        are kept, and how far back the oldest goes."""
        snapshot = self.data.latest_snapshot(default={}) or {}
        now = int(time.time())
        rows = []
        for row in snapshot.get('profiles') or []:
            oldest = row.get('oldest_epoch')
            rows.append({
                'path': row.get('path', '--'),
                'generation': str(row.get('generation') or '--'),
                'count': str(row.get('count') or 0),
                'oldest': format_age(now - int(oldest)) if oldest else '--',
            })
        return rows

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'last_gc_card': {'title': 'LAST COLLECTION', 'value_attr': '_last_gc_text',
                                 'color_attr': '_last_gc_color'},
                'freed_card': {'title': 'LAST YIELD', 'value_attr': '_freed_text',
                               'refresh': True},
                'next_gc_card': {'title': 'NEXT COLLECTION', 'value_attr': '_next_gc_text',
                                 'color_attr': '_next_gc_color'},
                'store_card': {'metric': 'store_used_pct', 'title': 'STORE USED',
                               'format': 'percent1', 'color': self._store_color},
                'paths_card': {'metric': 'store_paths', 'title': 'STORE PATHS',
                               'format': 'count_comma'},
                'generations_card': {'metric': 'generations', 'title': 'GENERATIONS',
                                     'format': 'int'},
                'roots_card': {'metric': 'gc_roots', 'title': 'GC ROOTS', 'format': 'count_comma'},
            },
            'tables': {
                'profiles': {
                    'row_key': 'path',
                    'rows_attr': '_profile_rows',
                    'columns': [
                        {'name': 'path', 'label': 'Profile', 'field': 'path', 'align': 'left'},
                        {'name': 'generation', 'label': 'Current', 'field': 'generation', 'align': 'right'},
                        {'name': 'count', 'label': 'Generations', 'field': 'count', 'align': 'right'},
                        {'name': 'oldest', 'label': 'Oldest kept', 'field': 'oldest', 'align': 'right'},
                    ],
                },
            },
            'chart': {'metric': 'store_paths', 'title': f'STORE PATHS — {self.store}'},
            'job_panel': {
                'widget': 'jobs',
                'title': 'COLLECTION JOBS',
                'run_action_id': 'collect',
                'run_label': 'Collect Garbage', 'run_icon': 'delete_sweep',
                'cancel_label': 'Cancel', 'cancel_icon': 'stop',
                'history_limit': 10,
            },
            'events': {'title': 'EVENTS', 'limit': 100, 'full_height': True},
        }
