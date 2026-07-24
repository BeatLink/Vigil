import json
import shlex
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin


def _folder_status_script(api_url: str, timeout: int, api_key_command: Optional[str],
                          api_key: Optional[str], folder_id: str) -> str:
    base = api_url.rstrip('/')
    if api_key_command:
        header = '-H "X-API-Key: $(' + api_key_command + ')"'
    else:
        header = f'-H {shlex.quote("X-API-Key: " + (api_key or ""))}'
    return f'curl -s -m {timeout} {header} {shlex.quote(base + "/rest/db/status?folder=" + folder_id)}'


_DEFAULT_LAYOUT = [
    ['host_card', 'folders_card', 'devices_card'],
    ['errors_card', 'need_card', 'stalled_card'],
    ['chart'],
    ['events'],
]


class SyncthingCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.api_url = config.get('api_url', 'http://127.0.0.1:8384')
        self.api_key = config.get('api_key')
        self.api_key_command = config.get(
            'api_key_command', 'cat /Storage/Services/Syncthing/Config/vigil-api-key')
        self.folders: Optional[List[str]] = config.get('folders') or None
        self.devices: Optional[List[str]] = config.get('devices') or None
        self.stall_warning_secs = float(config.get('stall_warning', 60)) * 60
        self.api_timeout = int(config.get('api_timeout', 10))
        self._stall_since: Dict[str, float] = {}

    async def _get_config(self) -> Optional[Dict[str, Any]]:
        script = (
            f'curl -s -m {self.api_timeout} '
            + (f'-H "X-API-Key: $({self.api_key_command})"' if self.api_key_command
               else f'-H {shlex.quote("X-API-Key: " + (self.api_key or ""))}')
            + f' {shlex.quote(self.api_url.rstrip("/") + "/rest/system/config")}'
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            self.db_logger.write(f"Failed to query Syncthing config: {stderr.strip()}", level="ERROR")
            return None
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            self.db_logger.write(f"Config response was not JSON ({e})", level="ERROR")
            return None

    async def on_collect(self):
        cfg = await self._get_config()
        if cfg is None:
            self.set_status('failed')
            return

        all_folder_ids = [f['id'] for f in cfg.get('folders', [])]
        watched_ids = [f for f in all_folder_ids
                       if self.folders is None or f in self.folders]

        if not watched_ids:
            self.db_logger.write("No matching folders configured in Syncthing", level="WARNING")
            self.set_status('warning')
            return

        import time as _time
        folder_states: Dict[str, Dict[str, Any]] = {}
        for folder_id in watched_ids:
            script = _folder_status_script(
                self.api_url, self.api_timeout, self.api_key_command,
                self.api_key, folder_id)
            ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
            if ret != 0:
                self.db_logger.write(
                    f"Failed to query folder {folder_id!r}: {stderr.strip()}", level="ERROR")
                self.set_status('failed')
                return
            try:
                folder_states[folder_id] = json.loads(stdout)
            except json.JSONDecodeError as e:
                self.db_logger.write(
                    f"Folder {folder_id!r} status was not JSON ({e})", level="ERROR")
                self.set_status('failed')
                return

        conn_script = (
            f'curl -s -m {self.api_timeout} '
            + (f'-H "X-API-Key: $({self.api_key_command})"' if self.api_key_command
               else f'-H {shlex.quote("X-API-Key: " + (self.api_key or ""))}')
            + f' {shlex.quote(self.api_url.rstrip("/") + "/rest/system/connections")}'
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(conn_script)
        if ret != 0:
            self.db_logger.write(f"Failed to query connections: {stderr.strip()}", level="ERROR")
            self.set_status('failed')
            return
        try:
            connections = json.loads(stdout).get('connections', {})
        except json.JSONDecodeError as e:
            self.db_logger.write(f"Connections response was not JSON ({e})", level="ERROR")
            self.set_status('failed')
            return

        device_names = {d['deviceID']: d.get('name', d['deviceID']) for d in cfg.get('devices', [])}
        expected_devices = [d for d in device_names
                             if self.devices is None or device_names[d] in self.devices
                             or d in (self.devices or [])]

        now = _time.monotonic()
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

        self.db_metrics.metric('folders_total', float(len(watched_ids)))
        self.db_metrics.metric('folders_errored', float(len(errored_folders)))
        self.db_metrics.metric('folders_stalled', float(len(stalled_folders)))
        self.db_metrics.metric('need_bytes', total_need_bytes)
        self.db_metrics.metric('pull_errors', float(total_pull_errors))

        disconnected = [
            device_names.get(dev_id, dev_id) for dev_id in expected_devices
            if not connections.get(dev_id, {}).get('connected', False)
        ]
        self.db_metrics.metric('devices_expected', float(len(expected_devices)))
        self.db_metrics.metric('devices_disconnected', float(len(disconnected)))

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
        self.db_logger.write(' | '.join(parts), level=log_level)
        self.set_status(level)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class SyncthingUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.spec import FORMATTERS
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )
        page = self.page(metric_names=[
            'folders_total', 'folders_errored', 'folders_stalled',
            'need_bytes', 'devices_expected', 'devices_disconnected',
        ])

        _int_or_dash = FORMATTERS['int']

        def _need_text(v):
            if v is None:
                return '--'
            return f'{v / (1024 * 1024):.1f} MiB'

        def _devices_text(_):
            exp_dev = page.model.metrics.get('devices_expected')
            disc = page.model.metrics.get('devices_disconnected')
            if exp_dev is None or disc is None:
                return '--'
            connected = int(exp_dev) - int(disc)
            return f'{connected}/{int(exp_dev)}'

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('folders_card'):
            info_card('FOLDERS', '--').bind_text_from(
                page.model, ('metrics', 'folders_total'), backward=_int_or_dash)
        with layout.cell('devices_card'):
            devices_label = info_card('DEVICES', '--').bind_text_from(
                page.model, ('metrics', 'devices_disconnected'), backward=_devices_text)
        with layout.cell('errors_card'):
            errors_label = info_card('ERRORED', '--').bind_text_from(
                page.model, ('metrics', 'folders_errored'), backward=_int_or_dash)
        with layout.cell('need_card'):
            info_card('NEED', '--').bind_text_from(
                page.model, ('metrics', 'need_bytes'), backward=_need_text)
        with layout.cell('stalled_card'):
            stalled_label = info_card('STALLED', '--').bind_text_from(
                page.model, ('metrics', 'folders_stalled'), backward=_int_or_dash)
        with layout.cell('chart'):
            history_chart(page, 'BYTES NEEDED', self.id, 'need_bytes')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_colors():
            disc = page.model.metrics.get('devices_disconnected')
            if disc is not None:
                devices_label.style(
                    f'color: {STATUS_COLORS["online" if disc == 0 else "warning"]}')

            errored = page.model.metrics.get('folders_errored')
            if errored is not None:
                errors_label.style(
                    f'color: {STATUS_COLORS["failed" if errored else "online"]}')

            stalled = page.model.metrics.get('folders_stalled')
            if stalled is not None:
                stalled_label.style(
                    f'color: {STATUS_COLORS["warning" if stalled else "online"]}')

        page.on_refresh(update_colors)
        page.start()
