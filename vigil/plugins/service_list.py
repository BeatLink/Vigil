import os
import shlex
from typing import Any, Dict, List, Optional, Union

from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.types import ActionPlan, CmdResult, Command, CollectResult
from vigil.plugins.base.web_plugin_base import UIPlugin

_DEFAULT_LAYOUT = [
    ['host_card', 'count_card', 'reload_card'],
    ['table'],
    ['events'],
]

_DEFAULT_UNIT_FILE_WRITE_PATHS = (
    '/etc/systemd/system',
    '/run/systemd/system',
    '/lib/systemd/system',
    '/usr/lib/systemd/system',
)


_LIST_UNITS_CMD = (
    'systemctl list-units --type=service --all '
    '--no-legend --no-pager --plain'
)
_LIST_UNIT_FILES_CMD = (
    'systemctl list-unit-files --type=service --all '
    '--no-legend --no-pager'
)


class ServiceListCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.max_logs = int(config.get('lines', 10))
        self.allow_unit_file_edit = bool(config.get('allow_unit_file_edit', False))

    def commands(self) -> List[Command]:
        return [Command(_LIST_UNITS_CMD), Command(_LIST_UNIT_FILES_CMD)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        units_result, unit_files_result = results

        if units_result.exit_code != 0:
            return CollectResult.failed(f'Collection failed: {units_result.stderr}')

        services = self._parse_unit_list(units_result.stdout)

        if unit_files_result.exit_code != 0:
            return CollectResult.failed(f'Unit-file collection failed: {unit_files_result.stderr}')

        enabled_map = self._parse_unit_file_list(unit_files_result.stdout)
        for service in services:
            service['enabled'] = enabled_map.get(service['unit'], 'unknown')

        total = len(services)
        active = sum(1 for s in services if s['active'] == 'active')
        failed = sum(1 for s in services if s['active'] == 'failed')

        return CollectResult(
            metrics={
                'services_total': float(total),
                'services_active': float(active),
                'services_failed': float(failed),
            },
            snapshot=services,
            logs=[(f'Collected {total} services, {active} active, {failed} failed', 'INFO')],
            status='online',
        )

    @staticmethod
    def _parse_unit_list(stdout: str) -> List[Dict[str, Any]]:
        services: List[Dict[str, Any]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            unit, load, active, sub, description = parts
            services.append({
                'unit': unit,
                'load': load,
                'active': active,
                'sub': sub,
                'description': description,
            })
        return services

    @staticmethod
    def _parse_unit_file_list(stdout: str) -> Dict[str, str]:
        enabled_map: Dict[str, str] = {}
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            unit_file = parts[0]
            state = parts[1]
            enabled_map[unit_file] = state
        return enabled_map

    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {
                'name': 'Reload Daemon',
                'action_id': 'daemon_reload',
                'variant': 'secondary',
                'icon': 'refresh',
            },
        ]

    def plan_action(self, action_id: str, **kwargs) -> Optional[Union[ActionPlan, CollectResult]]:
        service_name = kwargs.get('service_name')
        if action_id in ('start_service', 'stop_service', 'restart_service', 'enable_service', 'disable_service'):
            if not service_name:
                return CollectResult.failed('Service action missing service_name')
            command = action_id.replace('_service', '')
            return ActionPlan(f'sudo systemctl {command} {shlex.quote(service_name)}')

        if action_id == 'daemon_reload':
            return ActionPlan('sudo systemctl daemon-reload')

        if action_id == 'view_status':
            if not service_name:
                return CollectResult.failed('view_status missing service_name')
            return ActionPlan(f'sudo systemctl status {shlex.quote(service_name)} --no-pager')

        if action_id == 'view_unit_file':
            if not service_name:
                return CollectResult.failed('view_unit_file missing service_name')
            return ActionPlan(f'sudo systemctl cat {shlex.quote(service_name)}')

        if action_id == 'write_unit_file':
            if not service_name:
                return CollectResult.failed('write_unit_file missing service_name')
            import base64
            content_b64 = base64.b64encode(kwargs.get('content', '').encode('utf-8')).decode('ascii')
            allowed_write_paths = tuple(self.config.get('allowed_write_paths', _DEFAULT_UNIT_FILE_WRITE_PATHS))
            allowed_checks = ' || '.join(
                f'"$P" = {shlex.quote(p)} || case "$P" in {shlex.quote(p + os.sep)}*) true;; *) false;; esac'
                for p in allowed_write_paths
            )
            cmd = (
                f"P=$(systemctl show -p FragmentPath --value {shlex.quote(service_name)}); "
                "if [ -z \"$P\" ] || [ \"$P\" = 'n/a' ]; then echo 'Unit file path unavailable' >&2; exit 1; fi; "
                f"if ! ( {allowed_checks} ); then echo \"Write path not allowed: $P\" >&2; exit 1; fi; "
                f"echo {shlex.quote(content_b64)} | base64 -d | sudo tee \"$P\" > /dev/null"
            )
            return ActionPlan(cmd)

        return None

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        if action_id == 'view_status':
            if result.exit_code != 0:
                return CollectResult.failed(result.stderr.strip() or 'Unable to fetch service status')
            return CollectResult(success=True, metadata={'content': result.stdout})

        if action_id == 'view_unit_file':
            if result.exit_code != 0:
                return CollectResult.failed(result.stderr.strip() or 'Unable to read unit file')
            return CollectResult(success=True, metadata={'content': result.stdout})

        if action_id == 'write_unit_file':
            if result.exit_code != 0:
                return CollectResult.failed(result.stderr.strip() or 'Unable to write unit file')
            return CollectResult(logs=[('Unit file written successfully', 'INFO')], success=True)

        if result.exit_code != 0:
            command = action_id.replace('_service', '') if action_id != 'daemon_reload' else 'daemon-reload'
            service_name = kwargs.get('service_name')
            return CollectResult.failed(f'systemctl {command} failed for {service_name}: {result.stderr}')
        return True


class ServiceListUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.allow_unit_file_edit = bool(config.get('allow_unit_file_edit', False))

        from vigil.core.ui.ui.spec import register_enabled_predicate
        self._edit_predicate_name = f'service_list_edit_{self.id}'
        register_enabled_predicate(self._edit_predicate_name)(lambda p: p.allow_unit_file_edit)

    @property
    def _service_count_text(self) -> str:
        count_metric = self.storage.latest_metric('services_total')
        if count_metric is not None:
            return str(int(count_metric.value))
        return str(len(self.storage.latest_snapshot(default=[])))

    @property
    def UI_SPEC(self):
        row_actions = [
            {'id': 'start_service', 'icon': 'play_arrow', 'color': 'positive',
             'tooltip': 'Start Service', 'kind': 'dispatch', 'action_id': 'start_service',
             'params': {'service_name': 'unit'}},
            {'id': 'stop_service', 'icon': 'stop', 'color': 'warning',
             'tooltip': 'Stop Service', 'kind': 'dispatch', 'action_id': 'stop_service',
             'params': {'service_name': 'unit'}},
            {'id': 'restart_service', 'icon': 'replay', 'color': 'primary',
             'tooltip': 'Restart Service', 'kind': 'dispatch', 'action_id': 'restart_service',
             'params': {'service_name': 'unit'}},
            {'id': 'enable_service', 'icon': 'toggle_on', 'color': 'secondary',
             'tooltip': 'Enable on Boot', 'kind': 'dispatch', 'action_id': 'enable_service',
             'params': {'service_name': 'unit'}},
            {'id': 'disable_service', 'icon': 'toggle_off', 'color': 'secondary',
             'tooltip': 'Disable on Boot', 'kind': 'dispatch', 'action_id': 'disable_service',
             'params': {'service_name': 'unit'}},
            {'id': 'view_status', 'icon': 'info', 'color': 'info',
             'tooltip': 'View Status', 'kind': 'dialog', 'dialog': 'view_status'},
            {'id': 'view_file', 'icon': 'article', 'color': 'secondary',
             'tooltip': 'View Unit File', 'kind': 'dialog', 'dialog': 'view_unit_file'},
            {'id': 'edit_file', 'icon': 'edit', 'color': 'secondary',
             'tooltip': 'Edit Unit File', 'kind': 'dialog', 'dialog': 'edit_unit_file',
             'visible_if': self._edit_predicate_name},
        ]
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'count_card': {'title': 'SERVICES', 'value_attr': '_service_count_text'},
            },
            'buttons': {
                'reload_card': [
                    {'id': 'daemon_reload', 'label': 'Reload Daemon', 'icon': 'refresh',
                     'color': 'secondary', 'kind': 'dispatch'},
                ],
            },
            'tables': {
                'table': {
                    'row_key': 'unit',
                    'columns': [
                        {'name': 'unit', 'label': 'Unit', 'field': 'unit', 'sortable': True, 'align': 'left'},
                        {'name': 'load', 'label': 'Load', 'field': 'load', 'sortable': True, 'align': 'left'},
                        {'name': 'active', 'label': 'Active', 'field': 'active', 'sortable': True, 'align': 'left'},
                        {'name': 'sub', 'label': 'Sub', 'field': 'sub', 'sortable': True, 'align': 'left'},
                        {'name': 'enabled', 'label': 'Enabled', 'field': 'enabled', 'sortable': True, 'align': 'left'},
                        {'name': 'description', 'label': 'Description', 'field': 'description',
                         'sortable': True, 'align': 'left'},
                    ],
                    'row_actions': row_actions,
                },
            },
            'filters': {
                'table': {'placeholder': 'Filter services',
                          'fields': ['unit', 'load', 'active', 'sub', 'enabled', 'description']},
            },
            'dialogs': {
                'view_status': {'kind': 'read', 'title': 'Status: {row[unit]}',
                                'action_id': 'view_status', 'params': {'service_name': 'unit'},
                                'render': 'text'},
                'view_unit_file': {'kind': 'read', 'title': 'Unit File: {row[unit]}',
                                   'action_id': 'view_unit_file', 'params': {'service_name': 'unit'},
                                   'render': 'textarea_readonly'},
                'edit_unit_file': {'kind': 'edit', 'title': 'Edit Unit File: {row[unit]}',
                                   'load_action_id': 'view_unit_file', 'load_params': {'service_name': 'unit'},
                                   'save_action_id': 'write_unit_file', 'save_params': {'service_name': 'unit'},
                                   'save_content_kwarg': 'content',
                                   'success_message': 'Unit file written successfully'},
            },
            'events': {'title': 'PLUGIN EVENTS'},
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.ui.spec import generic_render
        generic_render(self, context)
