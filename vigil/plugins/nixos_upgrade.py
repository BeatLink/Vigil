"""NixOS flake deployment drift: whether the running system is the one the
configured flake currently evaluates to. Every cycle reads the target's
/run/current-system, /run/booted-system, generation and version (one cheap
script); every `eval_interval` it additionally evaluates
`<flake>#nixosConfigurations.<host>.config.system.build.toplevel` and reads
`nix flake metadata`, so drift is a comparison of two store paths rather than
a guess from revisions, and the flake's own revision and input ages are
tracked alongside it. Actions launch detached jobs on the target: `nix flake
update` on the flake, and `nixos-rebuild switch --flake`. Config: flake,
configuration, eval_interval, eval_timeout, max_input_age, drift_status,
reboot_status, require_sudo, nix_bin, rebuild_bin, nix_args, rebuild_args."""

import json
import shlex
import time
from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors import ssh_connector as detached
from vigil.core.connectors.types import (
    ActionPlan, CmdResult, Command, CollectResult, Status,
)
from vigil.plugins.base.plugin_helpers import (
    StatusAccumulator, format_age, format_duration, parse_duration,
)


_DEFAULT_LAYOUT = [
    ['host_card', 'drift_card', 'reboot_card', 'generation_card'],
    ['flake_card', 'revision_card', 'switched_card', 'inputs_card'],
    ['details'],
    ['controls'],
    ['jobs'],
    ['events'],
]

# Metric carrying the evaluation's epoch as its value and the whole evaluated
# state as JSON metadata, so a restart resumes the eval schedule.
_STATE_METRIC = 'flake_eval_epoch'

# The parts of a system closure whose change between the booted and the current
# generation means the new one is only fully live after a reboot.
_BOOT_PARTS = ('initrd', 'kernel', 'kernel-modules', 'systemd')

_LOCAL_SCHEMES = ('path:', 'git+file://', 'file://')


def _probe_script() -> str:
    """One script collecting every cheap fact about the deployed system."""
    return '\n'.join([
        'echo "current=$(readlink -f /run/current-system 2>/dev/null)"',
        'echo "booted=$(readlink -f /run/booted-system 2>/dev/null)"',
        'echo "switched=$(stat -c %Y /run/current-system 2>/dev/null)"',
        'echo "profile=$(readlink /nix/var/nix/profiles/system 2>/dev/null)"',
        'echo "version=$(cat /run/current-system/nixos-version 2>/dev/null)"',
        "echo \"version_json=$(nixos-version --json 2>/dev/null | tr -d '\\n')\"",
        f'for part in {" ".join(_BOOT_PARTS)}; do',
        '  echo "booted_part=$(readlink -f /run/booted-system/$part 2>/dev/null)"',
        '  echo "current_part=$(readlink -f /run/current-system/$part 2>/dev/null)"',
        'done',
    ])


def _parse_probe(stdout: str) -> Dict[str, Any]:
    """Turn the probe script's key=value lines into a dict, collecting the
    repeated booted_part/current_part lines into lists."""
    fields: Dict[str, Any] = {'booted_part': [], 'current_part': []}
    for line in stdout.splitlines():
        key, sep, value = line.partition('=')
        if not sep:
            continue
        value = value.strip()
        if key in ('booted_part', 'current_part'):
            fields[key].append(value)
        else:
            fields[key] = value
    return fields


def _generation(profile_link: str) -> Optional[int]:
    """Extract the generation number from a `system-210-link` profile symlink."""
    name = profile_link.rsplit('/', 1)[-1]
    if not (name.startswith('system-') and name.endswith('-link')):
        return None
    try:
        return int(name[len('system-'):-len('-link')])
    except ValueError:
        return None


def _decode_json(raw: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _short_rev(revision: Optional[str]) -> str:
    """Abbreviate a flake revision, keeping the -dirty marker a local checkout
    with uncommitted changes carries."""
    if not revision:
        return '--'
    head, sep, tail = revision.partition('-')
    return head[:12] + sep + tail


def _escape(value: str) -> str:
    """Escape a value for embedding inside a double-quoted shell word."""
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')


def _severity(value, default: str) -> str:
    """Read a configured status name, falling back on anything unrecognised."""
    try:
        return str(Status(str(value)))
    except ValueError:
        return default


def _oldest_input(locks: Dict[str, Any]) -> Tuple[Optional[str], Optional[int]]:
    """Return the (name, lastModified) of the least recently updated locked
    input — the one `nix flake update` would move furthest."""
    nodes = locks.get('nodes')
    if not isinstance(nodes, dict):
        return None, None
    root = locks.get('root', 'root')
    oldest_name, oldest_epoch = None, None
    for name, node in nodes.items():
        if name == root or not isinstance(node, dict):
            continue
        locked = node.get('locked')
        stamp = locked.get('lastModified') if isinstance(locked, dict) else None
        if not isinstance(stamp, (int, float)):
            continue
        if oldest_epoch is None or stamp < oldest_epoch:
            oldest_name, oldest_epoch = name, int(stamp)
    return oldest_name, oldest_epoch


class NixosUpgrade(Plugin):
    def __init__(self, name: str, config):
        super().__init__(name, config)
        self.flake = str(config.get('flake', '/etc/nixos'))
        self.configuration = config.get('configuration')
        self.eval_interval = parse_duration(config.get('eval_interval', '1h'))
        self.eval_timeout = parse_duration(config.get('eval_timeout', '10m'))
        self.max_input_age = (
            parse_duration(config['max_input_age'])
            if config.get('max_input_age') is not None else None
        )
        self.drift_status = _severity(config.get('drift_status'), 'warning')
        self.reboot_status = _severity(config.get('reboot_status'), 'warning')
        self.require_sudo = bool(config.get('require_sudo', True))
        self.nix_bin = str(config.get('nix_bin', 'nix'))
        self.rebuild_bin = str(config.get('rebuild_bin', 'nixos-rebuild'))
        self.nix_args = list(config.get(
            'nix_args', ['--extra-experimental-features', 'nix-command flakes']))
        self.rebuild_args = list(config.get('rebuild_args', []))
        # Set by a finished job so the next cycle re-evaluates immediately
        # instead of waiting out eval_interval.
        self._force_eval = False
        self._pending_launch = None

    # --- flake reference ---

    @property
    def local_path(self) -> Optional[str]:
        """The flake's filesystem path when it is a local checkout, else None
        — `nix flake update` can only write a lock file it owns."""
        ref = self.flake
        for scheme in _LOCAL_SCHEMES:
            if ref.startswith(scheme):
                ref = ref[len(scheme):]
                break
        else:
            if not (ref.startswith('/') or ref.startswith('.')):
                return None
        path = ref.split('?', 1)[0].split('#', 1)[0]
        return path or None

    def _refresh_args(self) -> List[str]:
        """A mutable remote ref is only seen freshly with --refresh; a local
        path is read from disk every time and needs none."""
        return [] if self.local_path else ['--refresh']

    def _installable(self) -> str:
        """The flake installable for this host's system closure, double-quoted
        so an unset configuration falls back to the target's own hostname."""
        attr = _escape(str(self.configuration)) if self.configuration else '$(uname -n)'
        return (f'"{_escape(self.flake)}#nixosConfigurations.\\"{attr}\\"'
                f'.config.system.build.toplevel"')

    def _sudo(self) -> str:
        return 'sudo -n ' if self.require_sudo else ''

    # --- collection ---

    def commands(self) -> List[Command]:
        job = self._running_job()
        if job is not None:
            return [Command(detached.poll_command(job['workdir'], job['pid'], job['output_seq']))]

        commands = [Command(_probe_script())]
        if self._due_for_eval():
            nix = ' '.join([self.nix_bin] + [shlex.quote(a) for a in self.nix_args])
            refresh = ' '.join(self._refresh_args())
            commands.append(Command(
                ' '.join(filter(None, [nix, 'eval --raw --no-write-lock-file', refresh,
                                       self._installable()])),
                timeout=self.eval_timeout,
            ))
            commands.append(Command(
                ' '.join(filter(None, [nix, 'flake metadata --json --no-write-lock-file',
                                       refresh, shlex.quote(self.flake)])),
                timeout=self.eval_timeout,
            ))
        return commands

    def _running_job(self) -> Optional[dict]:
        job = self.jobs.running() if self.jobs else None
        if job and job.get('pid') and job.get('workdir'):
            return job
        return None

    def _state(self) -> Dict[str, Any]:
        """The last evaluation's stored result: target closure, flake revision
        and input ages, carried between cycles as metric metadata."""
        metric = self.data.latest_metric(_STATE_METRIC) if self.data else None
        return _decode_json(metric.metadata) if metric is not None and metric.metadata else {}

    def _due_for_eval(self) -> bool:
        if self._force_eval:
            return True
        evaluated = self._state().get('evaluated_epoch')
        if not evaluated:
            return True
        return time.time() - float(evaluated) >= self.eval_interval

    def parse(self, results: List[CmdResult]) -> CollectResult:
        job = self._running_job()
        if job is not None:
            return self._parse_poll(job, results[0])

        if not results:
            return CollectResult.failed('No probe result for this cycle', status='offline')

        probe = results[0]
        if probe.exit_code != 0 and not probe.stdout.strip():
            return CollectResult.failed(
                f"Could not read the deployed system: {(probe.stderr or probe.stdout).strip()[:200]}",
                status='offline')

        fields = _parse_probe(probe.stdout)
        current = fields.get('current')
        if not current:
            return CollectResult.failed(
                '/run/current-system is unreadable — is this a NixOS host?', status='offline')

        state = (self._eval_state(results[1], results[2]) if len(results) > 2
                 else self._state())
        return self._assemble(fields, current, state)

    def _eval_state(self, eval_result: CmdResult, metadata_result: CmdResult) -> Dict[str, Any]:
        """Fold this cycle's evaluation and flake metadata into the stored
        state, keeping the last known good values for whichever half failed."""
        state = dict(self._state())
        state['evaluated_epoch'] = int(time.time())
        self._force_eval = False

        if eval_result.exit_code == 0 and eval_result.stdout.strip():
            state['target'] = eval_result.stdout.strip().splitlines()[-1].strip()
            state['eval_error'] = None
        else:
            state['eval_error'] = (
                (eval_result.stderr or eval_result.stdout).strip()[:400]
                or f'nix eval exited {eval_result.exit_code}')

        data = _decode_json(metadata_result.stdout) if metadata_result.exit_code == 0 else {}
        if data:
            locked = data.get('locked') if isinstance(data.get('locked'), dict) else {}
            # An uncommitted local checkout has no `rev`, only a `dirtyRev`.
            state['flake_revision'] = (data.get('revision') or locked.get('rev')
                                       or locked.get('dirtyRev'))
            state['flake_last_modified'] = data.get('lastModified') or locked.get('lastModified')
            locks = data.get('locks') if isinstance(data.get('locks'), dict) else {}
            name, epoch = _oldest_input(locks)
            state['oldest_input'] = name
            state['inputs_last_modified'] = epoch
            state['metadata_error'] = None
        else:
            state['metadata_error'] = (
                (metadata_result.stderr or metadata_result.stdout).strip()[:400]
                or f'nix flake metadata exited {metadata_result.exit_code}')
        return state

    def _assemble(self, fields: Dict[str, Any], current: str,
                  state: Dict[str, Any]) -> CollectResult:
        """Turn the probe's facts and the stored evaluation into this cycle's
        metrics, log lines and worst-of status."""
        acc = StatusAccumulator()
        now = int(time.time())
        metrics: Dict[str, float] = {}
        metadata: Dict[str, str] = {}
        logs: List[Tuple[str, str]] = []

        version = fields.get('version') or _decode_json(
            fields.get('version_json', '')).get('nixosVersion') or 'unknown'
        generation = _generation(fields.get('profile', ''))
        if generation is not None:
            metrics['generation'] = float(generation)

        switched = fields.get('switched')
        if switched and switched.isdigit():
            metrics['last_switch_epoch'] = float(switched)
            logs.append((f"Running NixOS {version}, generation {generation or '?'}, "
                         f"switched {format_age(now - int(switched))}", 'INFO'))
        else:
            logs.append((f"Running NixOS {version}, generation {generation or '?'}", 'INFO'))

        target = state.get('target')
        if state.get('eval_error'):
            acc.escalate('failed')
            logs.append((f"Flake evaluation failed: {state['eval_error']}", 'ERROR'))
        if target:
            up_to_date = target == current
            metrics['up_to_date'] = 1.0 if up_to_date else 0.0
            if up_to_date:
                logs.append(('System matches the flake', 'INFO'))
            else:
                acc.escalate(self.drift_status)
                logs.append((f"System is out of date: {self.flake} evaluates to {target}, "
                             f"running {current}", Status(self.drift_status).log_level))

        if state.get('metadata_error'):
            acc.escalate('offline')
            logs.append((f"Could not read flake metadata: {state['metadata_error']}", 'WARNING'))
        metrics['flake_reachable'] = 0.0 if state.get('metadata_error') else 1.0

        flake_modified = state.get('flake_last_modified')
        if flake_modified:
            metrics['flake_last_modified_epoch'] = float(flake_modified)
            revision = state.get('flake_revision')
            logs.append((f"Flake last changed {format_age(now - int(flake_modified))}"
                         + (f" at {_short_rev(revision)}" if revision else ''), 'INFO'))

        inputs_modified = state.get('inputs_last_modified')
        if inputs_modified:
            metrics['inputs_last_modified_epoch'] = float(inputs_modified)
            age = now - int(inputs_modified)
            stale = self.max_input_age is not None and age > self.max_input_age
            if stale:
                acc.escalate('warning')
            logs.append((
                f"Oldest locked input {state.get('oldest_input') or '?'} is "
                f"{format_age(age)}" + (
                    f", over the {format_duration(self.max_input_age)} limit" if stale else ''),
                'WARNING' if stale else 'INFO'))

        reboot = self._reboot_required(fields)
        if reboot is not None:
            metrics['reboot_required'] = 1.0 if reboot else 0.0
            if reboot:
                acc.escalate(self.reboot_status)
                logs.append(('Reboot required: the booted kernel/initrd is not the current one',
                             Status(self.reboot_status).log_level))

        metrics[_STATE_METRIC] = float(state.get('evaluated_epoch') or 0)
        metadata[_STATE_METRIC] = json.dumps(state)

        return CollectResult(
            metrics=metrics, metadata=metadata, logs=logs, status=str(acc.status),
            snapshot={'current': current, 'version': version,
                      'generation': generation, 'booted': fields.get('booted')},
        )

    @staticmethod
    def _reboot_required(fields: Dict[str, Any]) -> Optional[bool]:
        """Whether the booted closure's kernel-side parts differ from the
        current one's; None when the comparison could not be made."""
        booted, current = fields.get('booted_part') or [], fields.get('current_part') or []
        if not booted or len(booted) != len(current) or not any(booted):
            return None
        return booted != current

    # --- job polling ---

    def _parse_poll(self, job: dict, result: CmdResult) -> CollectResult:
        """Advance a running detached job from one poll's output, finalizing
        the Job row and forcing a re-evaluation when it completes."""
        job_id = job['id']
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

        kind = job['kind']
        self._force_eval = True
        if poll.exit_code is None:
            self.jobs.finish(job_id, 'failed', exit_code=-1,
                             error='Process ended without writing an exit code')
            return CollectResult.failed(f"{kind.capitalize()} ended unexpectedly")

        state = 'succeeded' if poll.exit_code == 0 else 'failed'
        self.jobs.finish(job_id, state, exit_code=poll.exit_code,
                         error=None if state == 'succeeded' else f"Exited with status {poll.exit_code}")
        if poll.exit_code == 0:
            return CollectResult(logs=[(f"{kind.capitalize()} completed successfully", 'INFO')],
                                 success=True)
        return CollectResult.failed(f"{kind.capitalize()} failed (exit {poll.exit_code})")

    # --- actions ---

    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {'name': 'Update Flake', 'action_id': 'update_flake',
             'variant': 'secondary', 'icon': 'sync'},
            {'name': 'Rebuild & Switch', 'action_id': 'switch',
             'variant': 'primary', 'icon': 'rocket_launch'},
        ]

    def _update_command(self) -> str:
        nix = ' '.join([self.nix_bin] + [shlex.quote(a) for a in self.nix_args])
        return f'{self._sudo()}{nix} flake update --flake {shlex.quote(self.flake)}'

    def _switch_command(self) -> str:
        flake_ref = f'{self.flake}#{self.configuration}' if self.configuration else self.flake
        args = ' '.join(shlex.quote(a) for a in self.rebuild_args + self._refresh_args())
        return (f'{self._sudo()}{self.rebuild_bin} switch --flake {shlex.quote(flake_ref)} '
                f'{args}').strip()

    def plan_action(self, action_id: str, **kwargs):
        if action_id not in ('update_flake', 'switch'):
            return None
        if self._running_job() is not None:
            return CollectResult.failed('A job is already running for this monitor',
                                        level='WARNING', status=None)
        if action_id == 'update_flake' and not self.local_path:
            return CollectResult.failed(
                f"Cannot update {self.flake}: only a local flake checkout has a lock file "
                f"this host can write")

        kind = 'update' if action_id == 'update_flake' else 'switch'
        command = self._update_command() if kind == 'update' else self._switch_command()
        # The on-target workdir is named before the Job row exists;
        # interpret_action records the pid the launch prints and creates the row.
        workdir = detached.workdir_for(f'{self.id}-{int(time.time())}')
        self._pending_launch = (kind, command, workdir)
        return ActionPlan(detached.launch_command(command, workdir))

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        if action_id not in ('update_flake', 'switch'):
            return result.exit_code == 0

        kind, command, workdir = self._pending_launch or ('switch', '', '')
        self._pending_launch = None

        pid = detached.parse_launch(result.stdout) if result.exit_code == 0 else None
        if pid is None:
            return CollectResult.failed(
                f"Failed to launch {kind}: {(result.stderr or result.stdout).strip()[:200]}")

        job_id = self.jobs.create(kind, command, workdir)
        self.jobs.set_pid(job_id, pid)
        return CollectResult(logs=[(f"{kind.capitalize()} started (pid {pid})", 'INFO')],
                             success=True)

    # --- UI ---

    def _metric(self, name: str) -> Optional[float]:
        metric = self.data.latest_metric(name)
        return metric.value if metric is not None else None

    def _drift_pair(self) -> Tuple[str, Optional[str]]:
        state = self._state()
        if state.get('eval_error'):
            return 'EVAL FAILED', 'failed'
        value = self._metric('up_to_date')
        if value is None:
            return 'UNKNOWN', 'offline'
        if value > 0.5:
            return 'UP TO DATE', 'online'
        return 'OUT OF DATE', self.drift_status

    def _reboot_pair(self) -> Tuple[str, Optional[str]]:
        value = self._metric('reboot_required')
        if value is None:
            return '--', 'offline'
        if value > 0.5:
            return 'REQUIRED', self.reboot_status
        return 'NOT NEEDED', 'online'

    def _inputs_pair(self) -> Tuple[str, Optional[str]]:
        epoch = self._metric('inputs_last_modified_epoch')
        if not epoch:
            return '--', None
        age = int(time.time()) - int(epoch)
        stale = self.max_input_age is not None and age > self.max_input_age
        return format_age(age), 'warning' if stale else 'online'

    @property
    def _drift_text(self) -> str:
        return self._drift_pair()[0]

    @property
    def _drift_color(self) -> Optional[str]:
        return self._drift_pair()[1]

    @property
    def _reboot_text(self) -> str:
        return self._reboot_pair()[0]

    @property
    def _reboot_color(self) -> Optional[str]:
        return self._reboot_pair()[1]

    @property
    def _inputs_text(self) -> str:
        return self._inputs_pair()[0]

    @property
    def _inputs_color(self) -> Optional[str]:
        return self._inputs_pair()[1]

    @property
    def _revision_text(self) -> str:
        return _short_rev(self._state().get('flake_revision'))

    @property
    def _switched_text(self) -> str:
        epoch = self._metric('last_switch_epoch')
        return format_age(int(time.time()) - int(epoch)) if epoch else '--'

    @property
    def _detail_rows(self) -> List[Dict[str, str]]:
        """The full deployment picture as label/value rows: what is running,
        what the flake says should be, and when each was last established."""
        state = self._state()
        snapshot = self.data.latest_snapshot(default={}) or {}
        now = int(time.time())

        def stamp(epoch) -> str:
            return format_age(now - int(epoch)) if epoch else '--'

        rows = [
            ('Flake', self.flake),
            ('Configuration', self.configuration or "the target's hostname"),
            ('NixOS version', snapshot.get('version', '--')),
            ('Generation', str(snapshot.get('generation') or '--')),
            ('Last switch', self._switched_text),
            ('Running closure', snapshot.get('current', '--')),
            ('Flake closure', state.get('target') or '--'),
            ('Flake revision', state.get('flake_revision') or '--'),
            ('Flake last changed', stamp(state.get('flake_last_modified'))),
            ('Oldest input', f"{state.get('oldest_input') or '--'} "
                             f"({stamp(state.get('inputs_last_modified'))})"),
            ('Last evaluated', stamp(state.get('evaluated_epoch'))),
        ]
        for label, error in (('Evaluation error', state.get('eval_error')),
                             ('Metadata error', state.get('metadata_error'))):
            if error:
                rows.append((label, error))
        return [{'label': label, 'value': value} for label, value in rows]

    def _update_enabled(self, _plugin) -> bool:
        return self.local_path is not None

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'drift_card': {'title': 'DEPLOYMENT', 'value_attr': '_drift_text',
                               'color_attr': '_drift_color'},
                'reboot_card': {'title': 'REBOOT', 'value_attr': '_reboot_text',
                                'color_attr': '_reboot_color'},
                'generation_card': {'metric': 'generation', 'title': 'GENERATION',
                                    'format': 'int'},
                'flake_card': {'title': 'FLAKE', 'value': self.flake},
                'revision_card': {'title': 'REVISION', 'value_attr': '_revision_text',
                                  'refresh': True},
                'switched_card': {'title': 'LAST SWITCH', 'value_attr': '_switched_text',
                                  'refresh': True},
                'inputs_card': {'title': 'OLDEST INPUT', 'value_attr': '_inputs_text',
                                'color_attr': '_inputs_color'},
            },
            'tables': {
                'details': {
                    'row_key': 'label',
                    'rows_attr': '_detail_rows',
                    'columns': [
                        {'name': 'label', 'label': 'Field', 'field': 'label', 'align': 'left'},
                        {'name': 'value', 'label': 'Value', 'field': 'value', 'align': 'left'},
                    ],
                },
            },
            'buttons': {
                'controls': [
                    {'id': 'update_flake', 'label': 'Update Flake', 'icon': 'sync',
                     'color': 'secondary', 'kind': 'dispatch',
                     'visible_if': self._update_enabled},
                ],
            },
            'job_panel': {
                'widget': 'jobs',
                'title': 'REBUILD JOBS',
                'run_action_id': 'switch',
                'run_label': 'Rebuild & Switch', 'run_icon': 'rocket_launch',
                'cancel_label': 'Cancel', 'cancel_icon': 'stop',
                'history_limit': 10,
            },
            'events': {'title': 'EVENTS', 'limit': 100, 'full_height': True},
        }
