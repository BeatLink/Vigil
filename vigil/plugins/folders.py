from typing import Dict, Any, List
from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import format_bytes as _format_gb


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

    def _level_for(self, gb: float, folder: Dict[str, Any]) -> str:
        threshold = folder.get('threshold')
        warning = folder.get('warning')
        if threshold is not None and gb >= float(threshold):
            return 'failed'
        if warning is not None and gb >= float(warning):
            return 'warning'
        return 'online'

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card
        from vigil.web.ui.spec import FORMATTERS
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.ui.page(metric_names=['worst_folder_gb'])

        by_key = {_sanitize(f.get('path', '')): f for f in self.folders if f.get('path')}

        _gb_or_dash = FORMATTERS['bytes_gb']

        with layout.cell('host_card'):
            self.ui.host_card()
        with layout.cell('count_card'):
            info_card('FOLDERS', str(len(self.folders)))
        with layout.cell('worst_card'):
            info_card('LARGEST', '--').bind_text_from(
                page.model, ('metrics', 'worst_folder_gb'), backward=_gb_or_dash)
        with layout.cell('folders'):
            folder_container = ui.element('div').style(
                'display: flex; flex-wrap: wrap; gap: 0.75rem; width: 100%'
            )
        with layout.cell('events'):
            self.ui.events_table(page)

        def update():
            folder_gb: Dict[str, float] = {}
            for row in (
                Metric.select()
                .where(
                    (Metric.collector == self.id) &
                    (Metric.metric_name.startswith('folder_')) &
                    (Metric.metric_name.endswith('_gb')) &
                    (Metric.metric_name != 'worst_folder_gb')
                )
                .order_by(Metric.timestamp.desc())
                .limit(200)
            ):
                if row.metric_name not in folder_gb:
                    folder_gb[row.metric_name] = row.value

            folder_container.clear()
            with folder_container:
                for metric_name in sorted(folder_gb):
                    key = metric_name.removeprefix('folder_').removesuffix('_gb')
                    val = folder_gb[metric_name]
                    folder = by_key.get(key, {})
                    lbl = info_card(key.replace('_', '/'), _format_gb(val))
                    lbl.style(f'color: {STATUS_COLORS[self._level_for(val, folder)]}')

        page.on_refresh(update)
        update()
        page.start()
