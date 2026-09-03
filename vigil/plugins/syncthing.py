"""Syncthing sync health via its REST API over HTTP from the Vigil host:
system config and connections each cycle, plus per-folder /rest/db/status for
the folder IDs learned the cycle before. Config: api_url (required,
Vigil-reachable), api_key / api_key_command, folders, devices, stall_warning
(minutes), api_timeout. An errored or invalid folder — including one sitting
idle while still needing data — is failed, as is any API error; pull errors,
folders syncing or scanning past stall_warning, and expected devices
disconnected are warning. The local device, which the config lists but the
connections map never does, is not an expected device."""

import json
import time
from typing import Any, Dict, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CollectResult, HttpRequest, Request, Result,
)
from vigil.plugins.base.plugin_helpers import StatusAccumulator, resolve_secret


_DEFAULT_LAYOUT = [
    ['host_card', 'folders_card', 'devices_card'],
    ['errors_card', 'need_card', 'stalled_card'],
    ['chart'],
    ['events'],
]


def _decode_config(config_result) -> Dict[str, Any]:
    """Decode /rest/system/config, raising ValueError with the user-facing message."""
    if config_result.error is not None:
        raise ValueError(f"Failed to query Syncthing config: {config_result.error}")
    if config_result.status_code != 200:
        raise ValueError(f"Syncthing config returned HTTP {config_result.status_code} "
                         f"(check the API key)")
    try:
        return json.loads(config_result.text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Config response was not JSON ({e})") from e


def _decode_folder_states(watched_ids: List[str], folder_results) -> Dict[str, Dict[str, Any]]:
    """Decode each /rest/db/status reply, raising ValueError with the user-facing message."""
    folder_states: Dict[str, Dict[str, Any]] = {}
    for folder_id, result in zip(watched_ids, folder_results):
        if result.error is not None or result.status_code != 200:
            detail = result.error or f"HTTP {result.status_code}"
            raise ValueError(f"Failed to query folder {folder_id!r}: {detail}")
        try:
            folder_states[folder_id] = json.loads(result.text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Folder {folder_id!r} status was not JSON ({e})") from e
    return folder_states


def _decode_connections(connections_result) -> Dict[str, Any]:
    """Decode /rest/system/connections, raising ValueError with the user-facing message."""
    if connections_result.error is not None or connections_result.status_code != 200:
        detail = connections_result.error or f"HTTP {connections_result.status_code}"
        raise ValueError(f"Failed to query connections: {detail}")
    try:
        return json.loads(connections_result.text).get('connections', {})
    except json.JSONDecodeError as e:
        raise ValueError(f"Connections response was not JSON ({e})") from e


def _watched_folder_ids(config: Dict[str, Any], folders_filter: Optional[List[str]]) -> List[str]:
    """The folder IDs Syncthing reports, narrowed to the watch list when one is set."""
    all_folder_ids = [folder['id'] for folder in config.get('folders', [])]
    return [folder_id for folder_id in all_folder_ids
            if folders_filter is None or folder_id in folders_filter]


def _expected_device_ids(device_names: Dict[str, str],
                         devices_filter: Optional[List[str]],
                         connections: Dict[str, Any]) -> List[str]:
    """Device IDs expected to be connected: all remote ones, or those matching the watch list by name or ID.

    The config's device list includes the local device, which never has a
    connection entry, so only devices Syncthing reports in /rest/system/connections count."""
    return [device_id for device_id in device_names
            if device_id in connections
            and (devices_filter is None or device_names[device_id] in devices_filter
                 or device_id in (devices_filter or []))]


def _disconnected_names(device_names: Dict[str, str], expected_devices: List[str],
                        connections: Dict[str, Any]) -> List[str]:
    """Names of expected devices without a live connection."""
    return [device_names.get(device_id, device_id) for device_id in expected_devices
            if not connections.get(device_id, {}).get('connected', False)]


def _sync_metrics(watched_count: int, errored: List[str], stalled: List[str],
                  need_bytes: float, pull_errors: int, expected_count: int,
                  disconnected_count: int) -> Dict[str, float]:
    """The folder/device counts and totals as the metric dict."""
    return {
        'folders_total': float(watched_count),
        'folders_errored': float(len(errored)),
        'folders_stalled': float(len(stalled)),
        'need_bytes': need_bytes,
        'pull_errors': float(pull_errors),
        'devices_expected': float(expected_count),
        'devices_disconnected': float(disconnected_count),
    }


def _classify_folders(folder_states: Dict[str, Dict[str, Any]],
                      stall_since: Dict[str, float], now: float,
                      stall_warning_secs: float):
    """Sort folders into errored/stalled and total their need bytes and pull errors, returning a rebuilt stall-start map."""
    new_stall_since = dict(stall_since)
    errored_folders: List[str] = []
    stalled_folders: List[str] = []
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
            new_stall_since.pop(folder_id, None)
            continue

        if state == 'idle' and (need_files > 0 or need_bytes > 0):
            errored_folders.append(folder_id)
            continue

        if state in ('syncing', 'scanning'):
            started = new_stall_since.setdefault(folder_id, now)
            if now - started >= stall_warning_secs:
                stalled_folders.append(folder_id)
        else:
            new_stall_since.pop(folder_id, None)

    return errored_folders, stalled_folders, total_need_bytes, total_pull_errors, new_stall_since


def _accumulate_problems(errored_folders: List[str], stalled_folders: List[str],
                         disconnected: List[str], total_pull_errors: int,
                         stall_warning_secs: float) -> StatusAccumulator:
    """Fold errored/stalled folders, pull errors and disconnected devices into one worst-of status."""
    acc = StatusAccumulator()
    if errored_folders:
        acc.escalate('failed',
                     f"{len(errored_folders)} folder(s) errored: {', '.join(errored_folders[:3])}")
    if total_pull_errors > 0:
        acc.escalate('warning', f"{total_pull_errors} pull error(s)")
    if stalled_folders:
        acc.escalate('warning',
                     f"{len(stalled_folders)} folder(s) stalled >= {stall_warning_secs/60:.0f}m: "
                     f"{', '.join(stalled_folders[:3])}")
    if disconnected:
        acc.escalate('warning',
                     f"{len(disconnected)} device(s) disconnected: {', '.join(disconnected[:3])}")
    return acc


class Syncthing(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        # Vigil fetches this URL directly; it must be Vigil-reachable, no default.
        self.api_url = config.get('api_url')
        # Resolve the API key once, on the Vigil host, so requests() stays pure.
        self.api_key = resolve_secret(
            config.get('api_key'),
            config.get('api_key_command',
                       'cat /Storage/Services/Syncthing/Config/vigil-api-key'))
        self.folders: Optional[List[str]] = config.get('folders') or None
        self.devices: Optional[List[str]] = config.get('devices') or None
        self.stall_warning_secs = float(config.get('stall_warning', 60)) * 60
        self.api_timeout = int(config.get('api_timeout', 10))
        self._stall_since: Dict[str, float] = {}
        self._cached_folder_ids: List[str] = []

        self._devices_color = (
            lambda values: None if values.get('devices_disconnected') is None
            else ('online' if values['devices_disconnected'] == 0 else 'warning'))
        self._errored_color = (
            lambda errored: None if errored is None else ('failed' if errored else 'online'))
        self._stalled_color = (
            lambda stalled: None if stalled is None else ('warning' if stalled else 'online'))

    def _get(self, path: str) -> HttpRequest:
        base = self.api_url.rstrip('/')
        return HttpRequest(url=f"{base}{path}", timeout=self.api_timeout,
                           headers={"X-API-Key": self.api_key or ""})

    def requests(self) -> List[Request]:
        if not self.api_url:
            return []
        reqs = [
            self._get("/rest/system/config"),
            self._get("/rest/system/connections"),
        ]
        reqs += [self._get(f"/rest/db/status?folder={folder_id}")
                 for folder_id in self._cached_folder_ids]
        return reqs

    def parse_results(self, results: List[Result]) -> CollectResult:
        """Turns the [config, connections, *per-folder status] HTTP results into a
        CollectResult with folder/device/need-bytes metrics, one summary log line,
        and a worst-of status (errored folders failed; pull errors, stalls, and
        disconnected devices warning)."""
        if not results:
            return CollectResult.failed("No 'api_url' configured")

        config_result, connections_result = results[0], results[1]
        folder_results = results[2:]

        try:
            config = _decode_config(config_result)
        except ValueError as e:
            return CollectResult.failed(str(e))

        watched_ids = _watched_folder_ids(config, self.folders)
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

        try:
            folder_states = _decode_folder_states(watched_ids, folder_results)
            connections = _decode_connections(connections_result)
        except ValueError as e:
            return CollectResult.failed(str(e))

        device_names = {device['deviceID']: device.get('name', device['deviceID'])
                        for device in config.get('devices', [])}
        expected_devices = _expected_device_ids(device_names, self.devices, connections)
        disconnected = _disconnected_names(device_names, expected_devices, connections)

        (errored_folders, stalled_folders, total_need_bytes, total_pull_errors,
         self._stall_since) = _classify_folders(
            folder_states, self._stall_since, time.monotonic(), self.stall_warning_secs)

        metrics = _sync_metrics(len(watched_ids), errored_folders, stalled_folders,
                                total_need_bytes, total_pull_errors,
                                len(expected_devices), len(disconnected))

        acc = _accumulate_problems(errored_folders, stalled_folders, disconnected,
                                   total_pull_errors, self.stall_warning_secs)

        parts = [
            f"{len(watched_ids)} folder(s)",
            f"{len(expected_devices) - len(disconnected)}/{len(expected_devices)} devices connected",
        ]
        if acc.problems:
            parts.append("| " + "; ".join(acc.problems))

        return CollectResult(metrics=metrics, logs=[(' | '.join(parts), acc.log_level)], status=acc.status)

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
                                 'format_fn': self._devices_text, 'color_fn': self._devices_color},
                'errors_card': {'metric': 'folders_errored', 'title': 'ERRORED', 'format': 'int',
                                'color': self._errored_color},
                'need_card': {'title': 'NEED', 'metrics': ['need_bytes'], 'format_fn': self._need_text},
                'stalled_card': {'metric': 'folders_stalled', 'title': 'STALLED', 'format': 'int',
                                 'color': self._stalled_color},
            },
            'chart': {'metric': 'need_bytes', 'title': 'BYTES NEEDED'},
            'events': True,
        }

