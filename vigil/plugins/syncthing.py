import json
import shlex
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin

_MARK = "@@VIGIL_FOLDER@@"


def _auth_header(timeout: int, api_key_command: Optional[str], api_key: Optional[str]) -> str:
    if api_key_command:
        return '-H "X-API-Key: $(' + api_key_command + ')"'
    return f'-H {shlex.quote("X-API-Key: " + (api_key or ""))}'


def _config_script(api_url: str, timeout: int, api_key_command: Optional[str],
                   api_key: Optional[str]) -> str:
    header = _auth_header(timeout, api_key_command, api_key)
    base = api_url.rstrip('/')
    return f'curl -s -m {timeout} {header} {shlex.quote(base + "/rest/system/config")}'


def _connections_script(api_url: str, timeout: int, api_key_command: Optional[str],
                        api_key: Optional[str]) -> str:
    header = _auth_header(timeout, api_key_command, api_key)
    base = api_url.rstrip('/')
    return f'curl -s -m {timeout} {header} {shlex.quote(base + "/rest/system/connections")}'


def _folder_status_script(api_url: str, timeout: int, api_key_command: Optional[str],
                          api_key: Optional[str], folder_id: str) -> str:
    base = api_url.rstrip('/')
    header = _auth_header(timeout, api_key_command, api_key)
    return f'curl -s -m {timeout} {header} {shlex.quote(base + "/rest/db/status?folder=" + folder_id)}'


_DEFAULT_LAYOUT = [
    ['host_card', 'folders_card', 'devices_card'],
    ['errors_card', 'need_card', 'stalled_card'],
    ['chart'],
    ['events'],
]


class SyncthingCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.api_url = config.get('api_url', 'http://127.0.0.1:8384')
        self.api_key = config.get('api_key')
        self.api_key_command = config.get(
            'api_key_command', 'cat /Storage/Services/Syncthing/Config/vigil-api-key')
        self.folders: Optional[List[str]] = config.get('folders') or None
        self.devices: Optional[List[str]] = config.get('devices') or None
        self.stall_warning_secs = float(config.get('stall_warning', 60)) * 60
        self.api_timeout = int(config.get('api_timeout', 10))
        self._stall_since: Dict[str, float] = {}
        self._cached_folder_ids: List[str] = []

    def commands(self) -> List[Command]:
        cmds = [
            Command(_config_script(self.api_url, self.api_timeout, self.api_key_command, self.api_key)),
            Command(_connections_script(self.api_url, self.api_timeout, self.api_key_command, self.api_key)),
        ]
        cmds += [
            Command(_folder_status_script(
                self.api_url, self.api_timeout, self.api_key_command, self.api_key, folder_id))
            for folder_id in self._cached_folder_ids
        ]
        return cmds

    def parse(self, results: List[CmdResult]) -> CollectResult:
        config_result, connections_result = results[0], results[1]
        folder_results = results[2:]

        if config_result.exit_code != 0:
            return CollectResult.failed(
                f"Failed to query Syncthing config: {config_result.stderr.strip()}")
        try:
            cfg = json.loads(config_result.stdout)
        except json.JSONDecodeError as e:
            return CollectResult.failed(f"Config response was not JSON ({e})")

        all_folder_ids = [f['id'] for f in cfg.get('folders', [])]
        watched_ids = [f for f in all_folder_ids
                       if self.folders is None or f in self.folders]
        self._cached_folder_ids = watched_ids

        if not watched_ids:
            return CollectResult(
                logs=[("No matching folders configured in Syncthing", "WARNING")], status='warning')

        if not folder_results:
            # First cycle after startup / after the folder list changed: we
            # just learned the folder IDs, per-folder status lags one cycle.
            return CollectResult(
                logs=[(f"Discovered {len(watched_ids)} folder(s), fetching status next cycle", "INFO")],
                status='warning',
            )

        folder_states: Dict[str, Dict[str, Any]] = {}
        for folder_id, result in zip(watched_ids, folder_results):
            if result.exit_code != 0:
                return CollectResult.failed(f"Failed to query folder {folder_id!r}: {result.stderr.strip()}")
            try:
                folder_states[folder_id] = json.loads(result.stdout)
            except json.JSONDecodeError as e:
                return CollectResult.failed(f"Folder {folder_id!r} status was not JSON ({e})")

        if connections_result.exit_code != 0:
            return CollectResult.failed(f"Failed to query connections: {connections_result.stderr.strip()}")
        try:
            connections = json.loads(connections_result.stdout).get('connections', {})
        except json.JSONDecodeError as e:
            return CollectResult.failed(f"Connections response was not JSON ({e})")

        device_names = {d['deviceID']: d.get('name', d['deviceID']) for d in cfg.get('devices', [])}
        expected_devices = [d for d in device_names
                             if self.devices is None or device_names[d] in self.devices
                             or d in (self.devices or [])]

        now = time.monotonic()
        errored_folders = []
        stalled_folders = []
        total_need_bytes = 0.0
        total_pull_errors = 0

        for folder_id, status in folder_states.items():
            state = status.get('state', 'unknown')
            need_bytes = float(status.get('needBytes', 0) or 0)
            need_files = int(status.get('needFiles', 0) or 0)
            pull_errors = int(status.get('pullErrors', 0) or 0)
            invalid = status.get('invalid', '')

            total_need_bytes += need_bytes
            total_pull_errors += pull_errors

            if state == 'error' or invalid:
                errored_folders.append(folder_id)
                self._stall_since.pop(folder_id, None)
                continue

            if state == 'idle' and (need_files > 0 or need_bytes > 0):
                errored_folders.append(folder_id)
                continue

            if state in ('syncing', 'scanning'):
                started = self._stall_since.setdefault(folder_id, now)
                if now - started >= self.stall_warning_secs:
                    stalled_folders.append(folder_id)
            else:
                self._stall_since.pop(folder_id, None)

        metrics = {
            'folders_total': float(len(watched_ids)),
            'folders_errored': float(len(errored_folders)),
            'folders_stalled': float(len(stalled_folders)),
            'need_bytes': total_need_bytes,
            'pull_errors': float(total_pull_errors),
        }

        disconnected = [
            device_names.get(dev_id, dev_id) for dev_id in expected_devices
            if not connections.get(dev_id, {}).get('connected', False)
        ]
        metrics['devices_expected'] = float(len(expected_devices))
        metrics['devices_disconnected'] = float(len(disconnected))

        problems = []
        level = 'online'

        def _escalate(new_level: str):
            nonlocal level
            order = ('online', 'warning', 'failed')
            if order.index(new_level) > order.index(level):
                level = new_level

        if errored_folders:
            problems.append(f"{len(errored_folders)} folder(s) errored: {', '.join(errored_folders[:3])}")
            _escalate('failed')
        if total_pull_errors > 0:
            problems.append(f"{total_pull_errors} pull error(s)")
            _escalate('warning')
        if stalled_folders:
            problems.append(
                f"{len(stalled_folders)} folder(s) stalled >= {self.stall_warning_secs/60:.0f}m: "
                f"{', '.join(stalled_folders[:3])}")
            _escalate('warning')
        if disconnected:
            problems.append(f"{len(disconnected)} device(s) disconnected: {', '.join(disconnected[:3])}")
            _escalate('warning')

        parts = [
            f"{len(watched_ids)} folder(s)",
            f"{len(expected_devices) - len(disconnected)}/{len(expected_devices)} devices connected",
        ]
        if problems:
            parts.append("| " + "; ".join(problems))

        log_level = "ERROR" if level == 'failed' else "WARNING" if level == 'warning' else "INFO"
        return CollectResult(metrics=metrics, logs=[(' | '.join(parts), log_level)], status=level)


class SyncthingUIPlugin(UIPlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        from vigil.web.ui.spec import register_color_rule, register_item_formatter
        self._devices_format_name = f'syncthing_devices_{self.id}'
        register_item_formatter(self._devices_format_name)(self._devices_text)
        self._need_format_name = f'syncthing_need_{self.id}'
        register_item_formatter(self._need_format_name)(self._need_text)

        self._devices_color_name = f'syncthing_devices_color_{self.id}'
        register_item_formatter(self._devices_color_name)(
            lambda values: None if values.get('devices_disconnected') is None
            else ('online' if values['devices_disconnected'] == 0 else 'warning'))
        self._errored_color_name = f'syncthing_errored_color_{self.id}'
        register_color_rule(self._errored_color_name)(
            lambda errored: None if errored is None else ('failed' if errored else 'online'))
        self._stalled_color_name = f'syncthing_stalled_color_{self.id}'
        register_color_rule(self._stalled_color_name)(
            lambda stalled: None if stalled is None else ('warning' if stalled else 'online'))

    @staticmethod
    def _need_text(values: Dict[str, Any]) -> str:
        v = values.get('need_bytes')
        if v is None:
            return '--'
        return f'{v / (1024 * 1024):.1f} MiB'

    @staticmethod
    def _devices_text(values: Dict[str, Any]) -> str:
        exp_dev, disc = values.get('devices_expected'), values.get('devices_disconnected')
        if exp_dev is None or disc is None:
            return '--'
        return f'{int(exp_dev) - int(disc)}/{int(exp_dev)}'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'folders_card': {'metric': 'folders_total', 'title': 'FOLDERS', 'format': 'int'},
                'devices_card': {'title': 'DEVICES', 'metrics': ['devices_expected', 'devices_disconnected'],
                                 'format_fn': self._devices_format_name, 'color_fn': self._devices_color_name},
                'errors_card': {'metric': 'folders_errored', 'title': 'ERRORED', 'format': 'int',
                                'color': self._errored_color_name},
                'need_card': {'title': 'NEED', 'metrics': ['need_bytes'], 'format_fn': self._need_format_name},
                'stalled_card': {'metric': 'folders_stalled', 'title': 'STALLED', 'format': 'int',
                                 'color': self._stalled_color_name},
            },
            'chart': {'metric': 'need_bytes', 'title': 'BYTES NEEDED'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
