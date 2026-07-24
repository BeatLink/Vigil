from typing import Dict, Any, List
from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult
from vigil.plugins.base.web_plugin_base import UIPlugin
from vigil.plugins.base.plugin_helpers import format_bytes as _format_gb


def _sanitize(path: str) -> str:
    s = path.strip('/')
    if not s:
        return 'root'
    return ''.join(c if c.isalnum() else '_' for c in s)


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


_DEFAULT_LAYOUT = [
    ['host_card', 'count_card', 'worst_card'],
    ['folders'],
    ['events'],
]


class FoldersCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.folders = config.get('folders', []) or []
        self.du_timeout = int(config.get('timeout', 60))

    def _level_for(self, gb: float, folder: Dict[str, Any]) -> str:
        threshold = folder.get('threshold')
        warning = folder.get('warning')
        if threshold is not None and gb >= float(threshold):
            return 'failed'
        if warning is not None and gb >= float(warning):
            return 'warning'
        return 'online'

    def _valid_folders(self) -> List[Dict[str, Any]]:
        return [f for f in self.folders if f.get('path')]

    def commands(self) -> List[Command]:
        return [
            Command(f"timeout {self.du_timeout} du -sb {_shquote(folder['path'])}")
            for folder in self._valid_folders()
        ]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        if not self.folders:
            return CollectResult.failed("No folders configured", level="WARNING", status='offline')

        folders = self._valid_folders()

        severity = {'online': 0, 'warning': 1, 'failed': 2}
        worst_level = 'online'
        worst_gb = 0.0
        any_error = False
        metrics: Dict[str, float] = {}
        logs: List[tuple] = []

        for folder, result in zip(folders, results):
            path = folder['path']
            ret, stdout, stderr = result.exit_code, result.stdout, result.stderr

            if ret == 124:
                logs.append((f"{path}: du timed out after {self.du_timeout}s", "ERROR"))
                any_error = True
                continue
            if ret != 0:
                logs.append((f"{path}: du failed: {stderr.strip()}", "ERROR"))
                any_error = True
                continue

            try:
                size_bytes = int(stdout.split()[0])
            except (ValueError, IndexError):
                logs.append((f"{path}: could not parse du output {stdout.strip()!r}", "ERROR"))
                any_error = True
                continue

            gb = size_bytes / (1024 ** 3)
            metrics[f'folder_{_sanitize(path)}_gb'] = gb
            level = self._level_for(gb, folder)
            worst_gb = max(worst_gb, gb)
            if severity[level] > severity[worst_level]:
                worst_level = level
            logs.append((
                f"{path}: {_format_gb(gb)}",
                "ERROR" if level == 'failed' else "WARNING" if level == 'warning' else "INFO",
            ))

        metrics['worst_folder_gb'] = worst_gb

        status = 'failed' if any_error else worst_level
        return CollectResult(metrics=metrics, logs=logs, status=status)


class FoldersUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.folders = config.get('folders', []) or []

        from vigil.core.ui.ui.spec import register_item_color_rule
        self._by_key = {_sanitize(f.get('path', '')): f for f in self.folders if f.get('path')}
        self._color_rule_name = f'folders_threshold_{self.id}'
        register_item_color_rule(self._color_rule_name)(self._item_color)

    def _item_color(self, item: Dict[str, Any]) -> str:
        gb = item.get('value') or 0.0
        folder = self._by_key.get(item.get('key', ''), {})
        threshold = folder.get('threshold')
        warning = folder.get('warning')
        if threshold is not None and gb >= float(threshold):
            return 'failed'
        if warning is not None and gb >= float(warning):
            return 'warning'
        return 'online'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'count_card': {'title': 'FOLDERS', 'value': str(len(self.folders))},
                'worst_card': {'metric': 'worst_folder_gb', 'title': 'LARGEST', 'format': 'bytes_gb'},
                'folders': {
                    'repeat': {
                        'source': 'metrics_prefix',
                        'metrics_prefix': 'folder_', 'metrics_suffix': '_gb',
                        'metrics_exclude': ['worst_folder_gb'],
                        'item_format': 'bytes_gb',
                        'item_color_by': self._color_rule_name,
                        'label_transform': 'slashes',
                        'container': 'cards',
                        'empty_text': 'No folders configured',
                    },
                },
            },
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.ui.spec import generic_render
        generic_render(self, context)
