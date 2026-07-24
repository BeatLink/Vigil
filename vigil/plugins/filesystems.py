from typing import Dict, Any, List
from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import format_bytes as _format_gb

_EXCLUDE_TYPES = ['tmpfs', 'devtmpfs', 'squashfs', 'overlay', 'proc', 'sysfs',
                  'cgroup', 'cgroup2', 'devpts', 'mqueue', 'debugfs',
                  'tracefs', 'ramfs', 'efivarfs', 'configfs', 'fusectl',
                  'securityfs', 'pstore', 'autofs', 'binfmt_misc', 'nsfs']

_SNAP = '---SNAP---'

_RANK_UI = {'online': 0, 'warning': 1, 'failed': 2}


def _build_cmd() -> str:
    excludes = ' '.join(f"-x {t}" for t in _EXCLUDE_TYPES)
    space  = f"df -B1 {excludes} --output=target,size,used,pcent"
    inodes = f"df {excludes} --output=target,ipcent"
    return f"{space} && echo '{_SNAP}' && {inodes} && echo '{_SNAP}' && cat /proc/mounts"


def _sanitize(mountpoint: str) -> str:
    s = mountpoint.strip('/')
    if not s:
        return 'root'
    return ''.join(c if c.isalnum() else '_' for c in s)


def _parse_inodes(block: str) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for line in block.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            used_pct = float(fields[-1].rstrip('%'))
        except ValueError:
            continue
        result[' '.join(fields[:-1])] = used_pct
    return result


def _parse_readonly(block: str) -> Dict[str, bool]:
    result: Dict[str, bool] = {}
    for line in block.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        mountpoint = (fields[1].replace('\\040', ' ').replace('\\011', '\t')
                               .replace('\\012', '\n').replace('\\134', '\\'))
        result[mountpoint] = fields[3].split(',')[0] == 'ro'
    return result


_DEFAULT_LAYOUT = [
    ['host_card', 'count_card', 'worst_card'],
    ['filesystems'],
    ['events'],
]


class FilesystemsCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.warning   = int(config.get('warning',   80))
        self.threshold = int(config.get('threshold', 90))
        self.inode_warning   = int(config.get('inode_warning',   85))
        self.inode_threshold = int(config.get('inode_threshold', 95))
        self.readonly_is_failure = bool(config.get('readonly_is_failure', True))

    def _inode_level_for(self, pct: float) -> str:
        if pct >= self.inode_threshold:
            return 'failed'
        if pct >= self.inode_warning:
            return 'warning'
        return 'online'

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

        sections = stdout.split(_SNAP)
        inode_pct = _parse_inodes(sections[1]) if len(sections) > 1 else {}
        readonly  = _parse_readonly(sections[2]) if len(sections) > 2 else {}

        filesystems: List[tuple] = []
        for line in sections[0].splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4:
                continue
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
        worst_inode = 0.0
        overall = 'online'
        ro_mounts: List[str] = []

        def _escalate(level: str) -> None:
            nonlocal overall
            if _RANK_UI[level] > _RANK_UI[overall]:
                overall = level

        for mountpoint, used_pct, size_bytes, _used in filesystems:
            key = _sanitize(mountpoint)
            self.db_metrics.metric(f'fs_{key}_used_pct', used_pct)
            self.db_metrics.metric(f'fs_{key}_size_gb', size_bytes / (1024 ** 3))
            worst = max(worst, used_pct)
            level = self._level_for(used_pct)
            _escalate(level)
            if level != 'online':
                self.db_logger.write(
                    f"{mountpoint}: {used_pct:.0f}% used ({_format_gb(size_bytes / (1024**3))})",
                    level="ERROR" if level == 'failed' else "WARNING"
                )

            if mountpoint in inode_pct:
                ipct = inode_pct[mountpoint]
                self.db_metrics.metric(f'fs_{key}_inodes_pct', ipct)
                worst_inode = max(worst_inode, ipct)
                ilevel = self._inode_level_for(ipct)
                _escalate(ilevel)
                if ilevel != 'online':
                    self.db_logger.write(
                        f"{mountpoint}: {ipct:.0f}% of inodes used — writes may fail "
                        f"with ENOSPC despite free space",
                        level="ERROR" if ilevel == 'failed' else "WARNING"
                    )

            if readonly.get(mountpoint):
                ro_mounts.append(mountpoint)
                ro_level = 'failed' if self.readonly_is_failure else 'warning'
                _escalate(ro_level)
                self.db_logger.write(
                    f"{mountpoint}: mounted READ-ONLY — usage figures are stale; "
                    f"the kernel may have remounted it after an I/O error",
                    level="ERROR" if ro_level == 'failed' else "WARNING"
                )

        self.db_metrics.metric('worst_used_pct', worst)
        self.db_metrics.metric('worst_inodes_pct', worst_inode)
        self.db_metrics.metric('readonly_count', float(len(ro_mounts)))

        summary = (f"{len(filesystems)} filesystem(s), worst {worst:.0f}% "
                   f"(warn {self.warning}% / fail {self.threshold}%)")
        if inode_pct:
            summary += f", worst inodes {worst_inode:.0f}%"
        if ro_mounts:
            summary += f", {len(ro_mounts)} read-only: {', '.join(ro_mounts)}"
        self.db_logger.write(
            summary,
            level="INFO" if overall == 'online' else "WARNING" if overall == 'warning' else "ERROR"
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class FilesystemsUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.warning   = int(config.get('warning',   80))
        self.threshold = int(config.get('threshold', 90))
        self.inode_warning   = int(config.get('inode_warning',   85))
        self.inode_threshold = int(config.get('inode_threshold', 95))

    def _inode_level_for(self, pct: float) -> str:
        if pct >= self.inode_threshold:
            return 'failed'
        if pct >= self.inode_warning:
            return 'warning'
        return 'online'

    def _level_for(self, pct: float) -> str:
        if pct >= self.threshold:
            return 'failed'
        if pct >= self.warning:
            return 'warning'
        return 'online'

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=[])

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
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update():
            fs_pct: Dict[str, float] = {}
            fs_inodes: Dict[str, float] = {}
            for row in (
                Metric.select()
                .where(
                    (Metric.collector == self.id) &
                    (Metric.metric_name.startswith('fs_')) &
                    ((Metric.metric_name.endswith('_used_pct')) |
                     (Metric.metric_name.endswith('_inodes_pct')))
                )
                .order_by(Metric.timestamp.desc())
                .limit(400)
            ):
                target = fs_inodes if row.metric_name.endswith('_inodes_pct') else fs_pct
                key = (row.metric_name.removeprefix('fs_')
                                      .removesuffix('_inodes_pct')
                                      .removesuffix('_used_pct'))
                if key not in target:
                    target[key] = row.value

            fs_container.clear()
            with fs_container:
                for key in sorted(fs_pct):
                    val = fs_pct[key]
                    display = key.replace('_', '/')
                    display = '/' + display if display != 'root' else '/'
                    level = self._level_for(val)
                    text = f'{val:.0f}%'
                    ipct = fs_inodes.get(key)
                    if ipct is not None:
                        ilevel = self._inode_level_for(ipct)
                        if ilevel != 'online':
                            text += f'  ·  inodes {ipct:.0f}%'
                            if _RANK_UI[ilevel] > _RANK_UI[level]:
                                level = ilevel
                    lbl = info_card(display, text)
                    lbl.style(f'color: {STATUS_COLORS[level]}')

            worst_m = self.latest_metric('worst_used_pct')
            if worst_m is not None:
                worst_label.text = f'{worst_m.value:.0f}%'
                worst_label.style(f'color: {STATUS_COLORS[self._level_for(worst_m.value)]}')
            count_label.text = str(len(fs_pct))

        page.on_refresh(update)
        update()
        page.start()
