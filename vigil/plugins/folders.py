from typing import Dict, Any, List
from vigil.collector.plugin_base import CollectorPlugin
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
    """
    Monitors the size of arbitrary directories over SSH via `du`. Useful for
    watching things a filesystem check can't see — a growing log directory, a
    download spool, a media library approaching a soft cap.

    Each configured folder may set its own `warning`/`threshold` size (in GB);
    overall status is the worst level across all folders. A folder that can't be
    read (missing/permission) is reported failed.

    Config options:
      folders   List of { path, warning?, threshold? } entries. warning/threshold
                are sizes in GB; a folder over threshold => failed, over warning
                => warning. Both optional (a folder with neither is size-only).
      timeout   Per-du timeout in seconds (default: 60) — du can be slow on huge trees.
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.folders = config.get('folders', []) or []
        self.timeout = int(config.get('timeout', 60))

    def _level_for(self, gb: float, folder: Dict[str, Any]) -> str:
        threshold = folder.get('threshold')
        warning = folder.get('warning')
        if threshold is not None and gb >= float(threshold):
            return 'failed'
        if warning is not None and gb >= float(warning):
            return 'warning'
        return 'online'

    async def on_collect(self):
        if not self.folders:
            self.db_logger.write("No folders configured", level="WARNING")
            self.set_status('offline')
            return

        severity = {'online': 0, 'warning': 1, 'failed': 2}
        worst_level = 'online'
        worst_gb = 0.0
        any_error = False

        for folder in self.folders:
            path = folder.get('path')
            if not path:
                continue
            # `du -sb` gives total bytes for the tree; wrap in timeout for huge dirs.
            cmd = f"timeout {self.timeout} du -sb {_shquote(path)}"
            ret, stdout, stderr = await self.ssh_collector.fetch_output(cmd)

            if ret == 124:
                self.db_logger.write(f"{path}: du timed out after {self.timeout}s", level="ERROR")
                any_error = True
                continue
            if ret != 0:
                self.db_logger.write(f"{path}: du failed: {stderr.strip()}", level="ERROR")
                any_error = True
                continue

            try:
                size_bytes = int(stdout.split()[0])
            except (ValueError, IndexError):
                self.db_logger.write(f"{path}: could not parse du output {stdout.strip()!r}", level="ERROR")
                any_error = True
                continue

            gb = size_bytes / (1024 ** 3)
            self.db_metrics.metric(f'folder_{_sanitize(path)}_gb', gb)
            level = self._level_for(gb, folder)
            worst_gb = max(worst_gb, gb)
            if severity[level] > severity[worst_level]:
                worst_level = level
            self.db_logger.write(
                f"{path}: {_format_gb(gb)}",
                level="ERROR" if level == 'failed' else "WARNING" if level == 'warning' else "INFO"
            )

        self.db_metrics.metric('worst_folder_gb', worst_gb)

        if any_error:
            self.set_status('failed')
        else:
            self.set_status(worst_level)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class FoldersUIPlugin(UIPlugin):
    """Dashboard rendering for the folders monitor."""

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

        # The 'folders' cell holds a dynamically-sized per-folder container
        # (one card per configured folder, queried live from Metric), which
        # doesn't fit UI_SPEC's fixed card model, so this stays a manual
        # layout+page build — reusing the shared 'bytes_gb' formatter rather
        # than redefining it.
        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['worst_folder_gb'])

        # Map sanitized metric suffix -> folder config for threshold coloring.
        by_key = {_sanitize(f.get('path', '')): f for f in self.folders if f.get('path')}

        _gb_or_dash = FORMATTERS['bytes_gb']

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
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
            self.internal_modules['ui']['events_table'](page)

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
