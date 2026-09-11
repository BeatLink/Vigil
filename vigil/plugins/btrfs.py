"""Btrfs filesystem health and capacity, via btrfs-progs."""

from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.signal_plugin import SignalPlugin, worst_status as _worst
from vigil.core.connectors.types import CmdResult, CollectResult, Command, Status
from vigil.core.settings.config_schema import PluginConfig
from vigil.plugins.base.plugin_helpers import format_bytes, level_for


# One mountpoint per filesystem — the shortest, so a container with subvolumes
# mounted all over the tree is reported once under its topmost mount rather
# than once per subvolume. Every command here reads through the mountpoint, and
# a mountpoint read needs no privilege; only `btrfs filesystem show` and the
# per-device chunk detail want root, and neither is used.
_PROBE = (
    "command -v btrfs >/dev/null 2>&1 || { echo 'ERROR btrfs-progs not found'; exit 1; }; "
    "findmnt -nrt btrfs -o UUID,TARGET 2>/dev/null "
    "| awk 'NF == 2 { if (!($1 in m) || length($2) < length(m[$1])) m[$1] = $2 } "
    "       END { for (u in m) print m[u] }' "
    "| sort | while read -r mnt; do "
    "  echo \"FS $mnt\"; "
    "  btrfs filesystem usage -b \"$mnt\" 2>/dev/null | awk '"
    "    /^[ \\t]*Device size:/ { print \"USE size \" $NF } "
    "    /^[ \\t]*Device unallocated:/ { print \"USE unallocated \" $NF } "
    "    /^[ \\t]*Used:/ { print \"USE used \" $NF }'; "
    "  btrfs device stats \"$mnt\" 2>/dev/null | sed 's/^/DEV /'; "
    "done"
)

_GIB = 1024 ** 3


def _sanitize_mount(mountpoint: str) -> str:
    """A mountpoint reduced to the characters a metric name may carry."""
    stripped = mountpoint.strip('/')
    if not stripped:
        return 'root'
    return ''.join(c if c.isalnum() else '_' for c in stripped)


class _Filesystem:
    """One btrfs filesystem's readings, as the probe reported them."""

    def __init__(self, mountpoint: str):
        self.mountpoint = mountpoint
        self.key = _sanitize_mount(mountpoint)
        self.size = 0
        self.used = 0
        self.unallocated = 0
        self.errors: Dict[str, int] = {}

    @property
    def usage_pct(self) -> float:
        """How much of the raw device the filesystem occupies.

        Raw terms on both sides, so a DUP or RAID1 profile — which doubles what
        a byte of data costs — reads as the fuller filesystem it really is."""
        return 100.0 * self.used / self.size if self.size else 0.0

    @property
    def error_count(self) -> int:
        return sum(self.errors.values())

    @property
    def error_summary(self) -> str:
        return ', '.join(f"{name} {count}" for name, count in sorted(self.errors.items()) if count)


def _parse_probe(stdout: str) -> List[_Filesystem]:
    """Split the probe's FS/USE/DEV lines into one _Filesystem per mountpoint."""
    filesystems: List[_Filesystem] = []
    current: Optional[_Filesystem] = None
    for line in stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == 'FS' and len(parts) == 2:
            current = _Filesystem(parts[1])
            filesystems.append(current)
        elif parts[0] == 'USE' and len(parts) == 3 and current is not None:
            if parts[1] not in ('size', 'used', 'unallocated'):
                continue
            try:
                setattr(current, parts[1], int(parts[2]))
            except ValueError:
                continue
        elif parts[0] == 'DEV' and len(parts) == 3 and current is not None:
            counter = parts[1].rpartition('].')[2]
            try:
                current.errors[counter] = current.errors.get(counter, 0) + int(parts[2])
            except ValueError:
                continue
    return [fs for fs in filesystems if fs.size]


class Btrfs(SignalPlugin):
    """Btrfs filesystem state and capacity from one probe, reporting a count of
    filesystems carrying device errors plus a usage metric per filesystem and
    the fullest one's usage — the btrfs sibling of the zfs monitor.

    A pool's health word has no btrfs equivalent, so the verdict comes from the
    error counters btrfs keeps per device: read, write, flush, corruption and
    generation failures, counted since the filesystem was made and cleared only
    by hand. Any of them above zero is this monitor's DEGRADED."""

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.warning = float(config.get('warning', 80))
        self.threshold = float(config.get('threshold', 90))
        self.filesystems = list(config.get('filesystems') or [])
        self.unallocated_warning = int(config.get('unallocated_warning', _GIB))

        from vigil.core.ui.spec import (
            register_color_rule, register_item_color_rule, register_item_formatter,
            threshold_color,
        )
        self._color_rule = f'btrfs_usage_{self.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))
        self._item_color_rule = f'btrfs_fs_{self.id}'
        register_item_color_rule(self._item_color_rule)(self._item_level)
        self._item_format_fn = f'btrfs_text_{self.id}'
        register_item_formatter(self._item_format_fn)(self._item_text)

    SAMPLED = True

    def commands(self) -> List[Command]:
        return [Command(_PROBE)]

    def _level_for(self, fs: _Filesystem) -> str:
        """One filesystem's verdict: errors outrank fullness, and a container
        with nothing left to allocate is a warning however empty it reads."""
        if fs.error_count:
            return 'failed'
        level = level_for(fs.usage_pct, self.warning, self.threshold)
        if fs.unallocated < self.unallocated_warning:
            return _worst([level, 'warning'])
        return level

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0 and not stdout.strip():
            return CollectResult.failed(f"btrfs probe failed: {stderr or stdout}")
        if stdout.startswith('ERROR '):
            return CollectResult.failed(stdout.strip())

        filesystems = _parse_probe(stdout)
        if self.filesystems:
            filesystems = [fs for fs in filesystems if fs.mountpoint in self.filesystems]
        if not filesystems:
            return CollectResult(logs=[("No btrfs filesystems found", "WARNING")], status='offline')

        metrics: Dict[str, float] = {}
        logs: List[Tuple[str, str]] = []
        statuses: List[str] = []
        ok = degraded = 0

        for fs in filesystems:
            level = self._level_for(fs)
            statuses.append(level)
            if fs.error_count:
                degraded += 1
            else:
                ok += 1

            metrics[f'fs_usage_{fs.key}'] = fs.usage_pct
            metrics[f'fs_errors_{fs.key}'] = float(fs.error_count)
            metrics[f'fs_unallocated_{fs.key}'] = float(fs.unallocated)

            detail = (f"{fs.usage_pct:.0f}% used of {format_bytes(fs.size / _GIB)} "
                      f"(warn {self.warning:g}% / fail {self.threshold:g}%)")
            if fs.error_count:
                detail += f", device errors: {fs.error_summary}"
            elif fs.unallocated < self.unallocated_warning:
                detail += (f", only {format_bytes(fs.unallocated / _GIB)} unallocated — "
                           f"btrfs cannot make a new chunk and writes may fail with ENOSPC")
            logs.append((f"{fs.mountpoint}: {detail}", Status(level).log_level))

        metrics.update({
            'filesystems_total': float(ok + degraded),
            'filesystems_ok': float(ok),
            'filesystems_degraded': float(degraded),
            'btrfs_usage_max': max(fs.usage_pct for fs in filesystems),
            'device_errors': float(sum(fs.error_count for fs in filesystems)),
        })

        return CollectResult(metrics=metrics, logs=logs, status=_worst(statuses))

    def _item_level(self, item: Dict[str, Any]) -> str:
        if (item.get('errors') or 0) > 0:
            return 'failed'
        level = level_for(item.get('usage_pct') or 0.0, self.warning, self.threshold)
        unallocated = item.get('unallocated')
        if unallocated is not None and unallocated < self.unallocated_warning:
            return _worst([level, 'warning'])
        return level

    def _item_text(self, item: Dict[str, Any]) -> str:
        text = f"{item.get('usage_pct') or 0.0:.1f}%"
        errors = item.get('errors') or 0
        if errors:
            text += f"  ·  {int(errors)} dev err"
        return text

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'btrfs_total_card': {
                'metric': 'filesystems_total', 'title': 'FILESYSTEMS', 'format': 'int',
            },
            'btrfs_ok_card': {
                'metric': 'filesystems_ok', 'title': 'HEALTHY', 'format': 'int',
                'color': 'always_online',
            },
            'btrfs_degraded_card': {
                'metric': 'filesystems_degraded', 'title': 'DEGRADED', 'format': 'int',
                'color': 'nonzero_failed',
            },
            'btrfs_usage_card': {
                'metric': 'btrfs_usage_max', 'title': 'FULLEST FS',
                'format': 'percent1', 'color': self._color_rule,
            },
            'btrfs_filesystems': {
                'repeat': {
                    'source': 'metrics_prefix',
                    'fields': [
                        {'name': 'usage_pct', 'prefix': 'fs_usage_', 'suffix': ''},
                        {'name': 'errors', 'prefix': 'fs_errors_', 'suffix': ''},
                        {'name': 'unallocated', 'prefix': 'fs_unallocated_', 'suffix': ''},
                    ],
                    'item_format_fn': self._item_format_fn,
                    'item_color_by': self._item_color_rule,
                    'label_transform': 'slashes',
                    'container': 'cards',
                    'empty_text': 'No btrfs filesystems found',
                },
            },
        }

    def card_row(self) -> List[str]:
        return ['btrfs_total_card', 'btrfs_ok_card', 'btrfs_degraded_card', 'btrfs_usage_card']

    def rows(self) -> List[List[str]]:
        return [['btrfs_filesystems']]

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'btrfs_chart': {'metric': 'btrfs_usage_max', 'title': 'FULLEST FILESYSTEM (%)'}}
