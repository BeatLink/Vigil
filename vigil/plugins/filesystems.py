"""Space, inodes, and read-only state of every real filesystem on the target,
from one combined df/proc-mounts command over SSH — sampled locally by the
agent on agent-backed hosts. Config: warning / threshold (space percent),
inode_warning / inode_threshold, readonly_is_failure. The worst filesystem
sets the status: space or inode use past the warning level is warning and
past the threshold is failed, while a read-only mount is failed (warning when
readonly_is_failure is off) because the kernel may have remounted it after an
I/O error."""

from typing import Dict, Any, List
from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import CmdResult, CollectResult, Command, Status
from vigil.plugins.base.plugin_helpers import StatusAccumulator, format_bytes

_EXCLUDE_TYPES = ['tmpfs', 'devtmpfs', 'squashfs', 'overlay', 'proc', 'sysfs',
                  'cgroup', 'cgroup2', 'devpts', 'mqueue', 'debugfs',
                  'tracefs', 'ramfs', 'efivarfs', 'configfs', 'fusectl',
                  'securityfs', 'pstore', 'autofs', 'binfmt_misc', 'nsfs']

_SNAP = '---SNAP---'


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


def _parse_space(block: str) -> List[tuple]:
    """Parse the df space section into (mountpoint, used_pct, size_bytes, used_bytes) tuples."""
    filesystems: List[tuple] = []
    for line in block.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            used_pct = float(fields[-1].rstrip('%'))
            used_bytes = int(fields[-2])
            size_bytes = int(fields[-3])
        except (ValueError, IndexError):
            continue
        mountpoint = ' '.join(fields[:-3])
        filesystems.append((mountpoint, used_pct, size_bytes, used_bytes))
    return filesystems


def _assess_filesystems(filesystems: List[tuple], inode_pct: Dict[str, float],
                        readonly: Dict[str, bool], space_level_for, inode_level_for,
                        readonly_level: str):
    """Build per-filesystem metrics, problem logs, worst-usage figures, read-only mounts, and the worst-of status."""
    metrics: Dict[str, float] = {}
    logs: List[tuple] = []
    worst = 0.0
    worst_inode = 0.0
    acc = StatusAccumulator()
    ro_mounts: List[str] = []

    for mountpoint, used_pct, size_bytes, _used in filesystems:
        key = _sanitize(mountpoint)
        metrics[f'fs_{key}_used_pct'] = used_pct
        metrics[f'fs_{key}_size_gb'] = size_bytes / (1024 ** 3)
        worst = max(worst, used_pct)
        level = space_level_for(used_pct)
        acc.escalate(level)
        if level != 'online':
            logs.append((
                f"{mountpoint}: {used_pct:.0f}% used ({format_bytes(size_bytes / (1024**3))})",
                Status(level).log_level,
            ))

        if mountpoint in inode_pct:
            inode_used_pct = inode_pct[mountpoint]
            metrics[f'fs_{key}_inodes_pct'] = inode_used_pct
            worst_inode = max(worst_inode, inode_used_pct)
            inode_level = inode_level_for(inode_used_pct)
            acc.escalate(inode_level)
            if inode_level != 'online':
                logs.append((
                    f"{mountpoint}: {inode_used_pct:.0f}% of inodes used — writes may fail "
                    f"with ENOSPC despite free space",
                    Status(inode_level).log_level,
                ))

        if readonly.get(mountpoint):
            ro_mounts.append(mountpoint)
            acc.escalate(readonly_level)
            logs.append((
                f"{mountpoint}: mounted READ-ONLY — usage figures are stale; "
                f"the kernel may have remounted it after an I/O error",
                Status(readonly_level).log_level,
            ))

    return metrics, logs, worst, worst_inode, ro_mounts, acc


_DEFAULT_LAYOUT = [
    ['host_card', 'count_card', 'worst_card'],
    ['filesystems'],
    ['events'],
]


class Filesystems(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.warning   = int(config.get('warning',   80))
        self.threshold = int(config.get('threshold', 90))
        self.inode_warning   = int(config.get('inode_warning',   85))
        self.inode_threshold = int(config.get('inode_threshold', 95))
        self.readonly_is_failure = bool(config.get('readonly_is_failure', True))

        from vigil.core.ui.spec import register_item_color_rule, register_item_formatter
        self._color_rule_name = f'filesystems_level_{self.id}'
        register_item_color_rule(self._color_rule_name)(self._item_color)
        self._format_fn_name = f'filesystems_text_{self.id}'
        register_item_formatter(self._format_fn_name)(self._item_text)

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

    SAMPLED = True

    def commands(self) -> List[Command]:
        return [Command(_build_cmd())]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        """Turns the combined df/df-inodes/proc-mounts output into a CollectResult
        with per-filesystem usage metrics, a log line per problem plus a summary,
        and the worst filesystem's level as status."""
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0 and not stdout.strip():
            return CollectResult.failed(f"df failed: {stderr}")

        sections = stdout.split(_SNAP)
        inode_pct = _parse_inodes(sections[1]) if len(sections) > 1 else {}
        readonly  = _parse_readonly(sections[2]) if len(sections) > 2 else {}
        filesystems = _parse_space(sections[0])

        if not filesystems:
            return CollectResult.failed("No real filesystems found", level="WARNING", status='offline')

        readonly_level = 'failed' if self.readonly_is_failure else 'warning'
        metrics, logs, worst, worst_inode, ro_mounts, acc = _assess_filesystems(
            filesystems, inode_pct, readonly,
            self._level_for, self._inode_level_for, readonly_level)

        metrics['worst_used_pct'] = worst
        metrics['worst_inodes_pct'] = worst_inode
        metrics['readonly_count'] = float(len(ro_mounts))

        summary = (f"{len(filesystems)} filesystem(s), worst {worst:.0f}% "
                   f"(warn {self.warning}% / fail {self.threshold}%)")
        if inode_pct:
            summary += f", worst inodes {worst_inode:.0f}%"
        if ro_mounts:
            summary += f", {len(ro_mounts)} read-only: {', '.join(ro_mounts)}"
        logs.append((summary, acc.log_level))

        return CollectResult(metrics=metrics, logs=logs, status=acc.status)

    def _item_level(self, item: Dict[str, Any]) -> str:
        level = self._level_for(item.get('used_pct') or 0.0)
        ipct = item.get('inodes_pct')
        if ipct is not None:
            ilevel = self._inode_level_for(ipct)
            if Status(ilevel).severity > Status(level).severity:
                level = ilevel
        return level

    def _item_color(self, item: Dict[str, Any]) -> str:
        return self._item_level(item)

    def _item_text(self, item: Dict[str, Any]) -> str:
        val = item.get('used_pct') or 0.0
        text = f'{val:.0f}%'
        ipct = item.get('inodes_pct')
        if ipct is not None and self._inode_level_for(ipct) != 'online':
            text += f'  ·  inodes {ipct:.0f}%'
        return text

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'count_card': {'title': 'FILESYSTEMS', 'value_attr': '_filesystem_count'},
                'worst_card': {
                    'metric': 'worst_used_pct', 'title': 'WORST USAGE', 'format': 'percent0_plain_dash',
                },
                'filesystems': {
                    'repeat': {
                        'source': 'metrics_prefix',
                        'fields': [
                            {'name': 'used_pct', 'prefix': 'fs_', 'suffix': '_used_pct'},
                            {'name': 'inodes_pct', 'prefix': 'fs_', 'suffix': '_inodes_pct'},
                        ],
                        'item_format_fn': self._format_fn_name,
                        'item_color_by': self._color_rule_name,
                        'label_transform': 'slashes',
                        'container': 'cards',
                        'empty_text': 'No filesystems found',
                    },
                },
            },
            'events': True,
        }

    @property
    def _filesystem_count(self) -> str:
        from vigil.core.ui.components import _scan_metric_family
        return str(len(_scan_metric_family(self, 'fs_', '_used_pct', set(), 200)))

