"""
Syncthing folder/device health via the REST API.

Complements a `systemd_service` monitor on syncthing rather than replacing
it. That one answers "is the process alive"; this one answers "is
everything actually syncing", which is a different failure. The case that
motivates it: the daemon stays up and the GUI answers while a folder's sync
has stalled — a permission error on one file, a full disk, a device gone
unreachable — and every liveness check stays green while files quietly stop
propagating.

Two signals carry that:

  folder state    Syncthing's own per-folder state (`idle` / `scanning` /
                  `syncing` / `error` / ...), plus `needFiles`/`needBytes`.
                  A folder reporting `idle` with nonzero `needFiles` is a
                  known Syncthing anomaly (upstream issue #1765) meaning
                  sync isn't converging despite believing it's done —
                  checked explicitly because state alone would call it
                  healthy. `pullErrors` catches files that failed mid-sync
                  without ever moving the folder out of a nominally OK state.
  device connection Whether devices this instance expects to sync with are
                  actually connected, from `/rest/system/connections`. A
                  disconnected device means its share of every folder's
                  content is frozen, however healthy the local folder state
                  looks.

Both are read from Syncthing's own REST API rather than reasoned about from
first principles, because Syncthing already computes exactly these signals
for its own GUI — recomputing them from raw file lists would be redundant
and more likely to drift from what upstream considers "healthy".

The API key is not stored in Vigil's own config: it lives only in
Syncthing's config.xml, so a small Nix-managed timer (see syncthing.nix)
extracts just that value into a file `vigil-access` can read directly,
avoiding a shared credential store or wider file access.

Config options:
  api_url            Base URL of the Syncthing GUI/API, as seen from the
                     monitored host (default: http://127.0.0.1:8384)
  api_key_command    Command run on the monitored host whose stdout is the
                     API key (default: "cat /Storage/Services/Syncthing/
                     Config/vigil-api-key"). Prefer this over inlining the
                     key.
  api_key            API key, if not using api_key_command.
  folders            Folder IDs to judge. Empty (default) means every folder
                     Syncthing reports.
  devices            Device names/IDs expected to be connected. Empty
                     (default) means every configured device is expected
                     connected; a device you know is intentionally offline
                     (a laptop that's usually asleep) should be left out.
  stall_warning      Minutes a folder may sit in `syncing`/`scanning` before
                     status is warning (default: 60) — a folder legitimately
                     takes time on a large change, but this catches one that
                     stopped making progress rather than finishing.
  api_timeout        Seconds allowed for the remote curl calls (default: 10)
"""
import json
import shlex
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin


def _folder_status_script(api_url: str, timeout: int, api_key_command: Optional[str],
                          api_key: Optional[str], folder_id: str) -> str:
    """Syncthing has no bulk /rest/db/status — one call per folder id."""
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
    """Monitors Syncthing folder sync state and device connectivity via the REST API."""

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

        # One /rest/db/status call per folder — Syncthing has no bulk form.
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

        # --- folder judgment --------------------------------------------
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
                # Known anomaly: idle but not actually converged.
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

        # --- device judgment ---------------------------------------------
        disconnected = [
            device_names.get(dev_id, dev_id) for dev_id in expected_devices
            if not connections.get(dev_id, {}).get('connected', False)
        ]
        self.db_metrics.metric('devices_expected', float(len(expected_devices)))
        self.db_metrics.metric('devices_disconnected', float(len(disconnected)))

        # --- status ---------------------------------------------------------
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
    """Dashboard rendering for the syncthing monitor."""

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )
        page = self.page(metric_names=[
            'folders_total', 'folders_errored', 'folders_stalled',
            'need_bytes', 'devices_expected', 'devices_disconnected',
        ])

        def _int_or_dash(v):
            return '--' if v is None else str(int(v))

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
