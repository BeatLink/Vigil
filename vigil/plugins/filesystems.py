from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.plugin_utils import format_bytes as _format_gb
from vigil.core.ui.components import info_card, safe_timer
from vigil.core.ui.theme import STATUS_COLORS

# List every mounted filesystem in one shot: mountpoint, total, used, use%.
# -P = POSIX output (one line per FS, no wrapping); -B1 = bytes; -x excludes
# pseudo/virtual filesystems that just add noise.
_EXCLUDE_TYPES = ['tmpfs', 'devtmpfs', 'squashfs', 'overlay', 'proc', 'sysfs',
                  'cgroup', 'cgroup2', 'devpts', 'mqueue', 'debugfs',
                  'tracefs', 'ramfs', 'efivarfs', 'configfs', 'fusectl',
                  'securityfs', 'pstore', 'autofs', 'binfmt_misc', 'nsfs']


def _build_cmd() -> str:
    excludes = ' '.join(f"-x {t}" for t in _EXCLUDE_TYPES)
    # Header line is skipped in parsing; --output keeps the fields we need.
    return f"df -P -B1 {excludes} --output=target,size,used,pcent"


def _sanitize(mountpoint: str) -> str:
    """Turn a mountpoint into a safe metric-name suffix (e.g. '/var/log' -> 'var_log')."""
    s = mountpoint.strip('/')
    if not s:
        return 'root'
    return ''.join(c if c.isalnum() else '_' for c in s)


_DEFAULT_LAYOUT = [
    ['host_card', 'count_card', 'worst_card'],
    ['filesystems'],
    ['logs'],
]


class FilesystemsPlugin(BasePlugin):
    """
    Auto-discovers and monitors every mounted filesystem on the target over SSH
    via a single `df` call — no per-path configuration required. This is the
    fleet-wide counterpart to disk_space (which watches one explicit path).

    Pseudo/virtual filesystems (tmpfs, proc, cgroup, overlay, …) are excluded so
    only real storage shows up. Each filesystem gets a `fs_<mount>_used_pct`
    metric; overall status is the worst usage across all of them.

    Config options:
      warning    Usage % that triggers warning (default: 80)
      threshold  Usage % that triggers failed  (default: 90)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.warning   = int(config.get('warning',   80))
        self.threshold = int(config.get('threshold', 90))

    def _level_for(self, pct: float) -> str:
        if pct >= self.threshold:
            return 'failed'
        if pct >= self.warning:
            return 'warning'
        return 'online'

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(_build_cmd())
        if ret != 0 and not stdout.strip():
            self.db_logger.write(f"df failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        filesystems: List[tuple] = []  # (mountpoint, used_pct, size_bytes, used_bytes)
        for line in stdout.splitlines()[1:]:  # skip header
            fields = line.split()
            if len(fields) < 4:
                continue
            # target may contain spaces; the last three tokens are size/used/pcent.
            pcent = fields[-1]
            try:
                used_pct = float(pcent.rstrip('%'))
                used_bytes = int(fields[-2])
                size_bytes = int(fields[-3])
            except (ValueError, IndexError):
                continue
            mountpoint = ' '.join(fields[:-3])
            filesystems.append((mountpoint, used_pct, size_bytes, used_bytes))

        if not filesystems:
            self.db_logger.write("No real filesystems found", level="WARNING")
            self.set_status('offline')
            return

        worst = 0.0
        for mountpoint, used_pct, size_bytes, _used in filesystems:
            key = _sanitize(mountpoint)
            self.db_metrics.metric(f'fs_{key}_used_pct', used_pct)
            self.db_metrics.metric(f'fs_{key}_size_gb', size_bytes / (1024 ** 3))
            worst = max(worst, used_pct)
            level = self._level_for(used_pct)
            if level != 'online':
                self.db_logger.write(
                    f"{mountpoint}: {used_pct:.0f}% used ({_format_gb(size_bytes / (1024**3))})",
                    level="ERROR" if level == 'failed' else "WARNING"
                )

        self.db_metrics.metric('worst_used_pct', worst)
        overall = self._level_for(worst)
        self.db_logger.write(
            f"{len(filesystems)} filesystem(s), worst {worst:.0f}% "
            f"(warn {self.warning}% / fail {self.threshold}%)",
            level="INFO" if overall == 'online' else "WARNING" if overall == 'warning' else "ERROR"
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('count_card'):
            count_label = info_card('FILESYSTEMS', '--')
        with layout.cell('worst_card'):
            worst_label = info_card('WORST USAGE', '--')
        with layout.cell('filesystems'):
            fs_container = ui.element('div').style(
                'display: flex; flex-wrap: wrap; gap: 0.75rem; width: 100%'
            )
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update():
            # Latest used_pct per filesystem, deduplicated in Python from one query.
            fs_pct: Dict[str, float] = {}
            for row in (
                Metric.select()
                .where(
                    (Metric.collector == self.id) &
                    (Metric.metric_name.startswith('fs_')) &
                    (Metric.metric_name.endswith('_used_pct'))
                )
                .order_by(Metric.timestamp.desc())
                .limit(200)
            ):
                if row.metric_name not in fs_pct:
                    fs_pct[row.metric_name] = row.value

            fs_container.clear()
            with fs_container:
                for metric_name in sorted(fs_pct):
                    val = fs_pct[metric_name]
                    display = metric_name.removeprefix('fs_').removesuffix('_used_pct').replace('_', '/')
                    display = '/' + display if display != 'root' else '/'
                    lbl = info_card(display, f'{val:.0f}%')
                    lbl.style(f'color: {STATUS_COLORS[self._level_for(val)]}')

            worst_m = self.latest_metric('worst_used_pct')
            if worst_m is not None:
                worst_label.text = f'{worst_m.value:.0f}%'
                worst_label.style(f'color: {STATUS_COLORS[self._level_for(worst_m.value)]}')
            count_label.text = str(len(fs_pct))

        update()
        safe_timer(5.0, update)
