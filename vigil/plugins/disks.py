"""One monitor for a host's storage signals — SMART disk health, ZFS pool
state and capacity, mdadm array health, and disk I/O throughput — instead of
one plugin instance per signal.

Each signal is a module, configured and rendered independently via the
`modules` config block, following the same contract as `system_stats`. Every
module is opt-in — one that isn't declared is off — so a target only collects
(and only shows) what it is asked for. Modules stay pure: each declares its own
commands, parses its own results into metrics/logs/a status, and contributes
its own cards and charts. The plugin concatenates their commands, slices the
results back out positionally, and reports the worst module status as the
overall one.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.module_plugin import (
    LOG_LEVEL as _LOG_LEVEL, Module as _Module, ModularPlugin, SEVERITY as _SEVERITY,
    module_options, worst_status as _worst,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.plugins.base.plugin_helpers import level_for as _level_for


# ---------------------------------------------------------------------------
# SMART
# ---------------------------------------------------------------------------

# Classification is by positive assertion: any output other than an explicit
# PASSED/FAILED verdict means the check did not run, and a blind check must not
# read as a healthy disk.
_SMART_SCRIPT = (
    "command -v smartctl >/dev/null 2>&1 || { echo 'ERROR smartctl not found'; exit 1; }; "
    # zram, zvols, loop/md/device-mapper nodes are TYPE=disk to lsblk but have no SMART.
    "disks=$(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2==\"disk\"{print $1}' "
    "  | grep -Ev '^(zram|zd|loop|md|dm-|sr|fd|ram)' | sed 's|^|/dev/|'); "
    "[ -z \"$disks\" ] && exit 0; "
    "for d in $disks; do "
    "  transport=$(lsblk -no TRAN \"$d\" 2>/dev/null || echo ''); "
    "  if [ \"$transport\" = 'usb' ]; then "
    "    result=$(sudo smartctl -H -d sat \"$d\" 2>&1 || true); "
    "  else "
    "    result=$(sudo smartctl -H \"$d\" 2>&1 || true); "
    "  fi; "
    "  if echo \"$result\" | grep -iq 'test result: *PASSED'; then echo \"PASS $d\"; "
    "  elif echo \"$result\" | grep -iqE 'test result: *FAILED|SMART Health Status: *FAIL'; then echo \"FAIL $d\"; "
    "  elif echo \"$result\" | grep -iqE 'does not support SMART|Unable to detect device type|Operation not supported'; then "
    "    echo \"SKIP $d\"; "
    "  else echo \"UNKNOWN $d $(echo \"$result\" | tr '\\n' ' ' | cut -c1-160)\"; fi; "
    "done"
)


class _SmartModule(_Module):
    """Per-disk SMART overall-health verdicts from smartctl, counted into
    healthy/failed/unreadable. A disk whose health could not be read counts as
    failed, not healthy: "I cannot tell" and "it is fine" must not look alike."""

    key = 'smart'

    def commands(self) -> List[Command]:
        return [Command(_SMART_SCRIPT)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"SMART check script failed: {stdout or stderr}")

        passed, failed, unknown = 0, 0, 0
        logs = []
        for line in stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or parts[0] not in ('PASS', 'FAIL', 'UNKNOWN', 'SKIP'):
                continue
            result, rest = parts
            if result == 'SKIP':
                continue
            if result == 'FAIL':
                failed += 1
                logs.append((f"SMART failure detected on {rest}", "ERROR"))
            elif result == 'UNKNOWN':
                unknown += 1
                disk, _, detail = rest.partition(' ')
                logs.append((
                    f"Could not read SMART health for {disk}: {detail or 'no usable output'}",
                    "ERROR",
                ))
            else:
                passed += 1
                logs.append((f"SMART OK on {rest}", "INFO"))

        total = passed + failed + unknown
        if total == 0:
            return CollectResult(logs=[("No physical disks found", "WARNING")], status='offline')

        return CollectResult(
            metrics={
                'disks_total': total,
                'disks_ok': passed,
                'disks_failed': failed,
                'disks_unknown': unknown,
            },
            logs=logs,
            status='failed' if (failed > 0 or unknown > 0) else 'online',
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'smart_total_card': {'metric': 'disks_total', 'title': 'DISKS', 'format': 'int'},
            'smart_ok_card': {
                'metric': 'disks_ok', 'title': 'HEALTHY', 'format': 'int',
                'color': 'disks_always_online',
            },
            'smart_failed_card': {
                'metric': 'disks_failed', 'title': 'FAILED', 'format': 'int',
                'color': 'disks_nonzero_failed',
            },
        }


# ---------------------------------------------------------------------------
# ZFS
# ---------------------------------------------------------------------------

_UNHEALTHY = {'DEGRADED', 'FAULTED', 'OFFLINE', 'UNAVAIL', 'REMOVED'}


def _sanitize_pool(name: str) -> str:
    """A pool name reduced to the characters a metric name may carry."""
    return ''.join(c if c.isalnum() or c == '_' else '_' for c in name.lower())


class _ZfsModule(_Module):
    """ZFS pool state and capacity from one `zpool list`, reporting a count of
    degraded pools plus a usage metric per pool and the fullest pool's usage."""

    key = 'zfs'

    def __init__(self, plugin: 'Disks', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.warning = float(options.get('warning', 80))
        self.threshold = float(options.get('threshold', 90))
        self.pools = list(options.get('pools') or [])

        from vigil.core.ui.spec import register_item_color_rule, register_color_rule, threshold_color
        self._color_rule = f'disks_zfs_usage_{plugin.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))
        self._item_color_rule = f'disks_zfs_pool_{plugin.id}'
        register_item_color_rule(self._item_color_rule)(
            lambda item: _level_for(item.get('value') or 0.0, self.warning, self.threshold))

    def commands(self) -> List[Command]:
        return [Command("zpool list -H -o name,health,capacity " + " ".join(self.pools) + " 2>&1")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0 and not stdout.strip():
            return CollectResult.failed(f"zpool list failed: {stderr or stdout}")

        ok, degraded = 0, 0
        usage: Dict[str, float] = {}
        logs, statuses = [], []
        for line in stdout.splitlines():
            parts = line.strip().split()
            if len(parts) != 3:
                continue
            pool, health, capacity = parts
            try:
                usage_pct = float(capacity.rstrip('%'))
            except ValueError:
                continue
            usage[pool] = usage_pct

            if health in _UNHEALTHY:
                degraded += 1
                status = 'failed'
            else:
                ok += 1
                status = _level_for(usage_pct, self.warning, self.threshold)
            statuses.append(status)
            logs.append((
                f"Pool {pool}: {health}, {usage_pct:.0f}% used "
                f"(warn {self.warning:g}% / fail {self.threshold:g}%)",
                _LOG_LEVEL[status],
            ))

        if not usage:
            return CollectResult(logs=[("No ZFS pools found", "WARNING")], status='offline')

        metrics = {
            'pools_total': ok + degraded,
            'pools_ok': ok,
            'pools_degraded': degraded,
            'zfs_usage_max': max(usage.values()),
        }
        for pool, usage_pct in usage.items():
            metrics[f'pool_usage_{_sanitize_pool(pool)}'] = usage_pct

        return CollectResult(metrics=metrics, logs=logs, status=_worst(statuses))

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'zfs_total_card': {'metric': 'pools_total', 'title': 'POOLS', 'format': 'int'},
            'zfs_ok_card': {
                'metric': 'pools_ok', 'title': 'HEALTHY', 'format': 'int',
                'color': 'disks_always_online',
            },
            'zfs_degraded_card': {
                'metric': 'pools_degraded', 'title': 'DEGRADED', 'format': 'int',
                'color': 'disks_nonzero_failed',
            },
            'zfs_usage_card': {
                'metric': 'zfs_usage_max', 'title': 'FULLEST POOL',
                'format': 'percent1', 'color': self._color_rule,
            },
            'zfs_pools': {
                'repeat': {
                    'source': 'metrics_prefix',
                    'metrics_prefix': 'pool_usage_', 'metrics_suffix': '',
                    'item_format': 'percent1',
                    'item_color_by': self._item_color_rule,
                    'label_transform': 'spaces_upper',
                    'container': 'cards',
                    'empty_text': 'No ZFS pools found',
                },
            },
        }

    def card_row(self) -> List[str]:
        return ['zfs_total_card', 'zfs_ok_card', 'zfs_degraded_card', 'zfs_usage_card']

    def rows(self) -> List[List[str]]:
        return [['zfs_pools']]

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'zfs_chart': {'metric': 'zfs_usage_max', 'title': 'FULLEST POOL CAPACITY (%)'}}


# ---------------------------------------------------------------------------
# mdadm
# ---------------------------------------------------------------------------

_ARRAY_RE = re.compile(r'^(md\d+)\s*:\s*(\S+)\s+(\S+)', re.MULTILINE)
_STATE_RE = re.compile(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]')
_RECOVERY_RE = re.compile(r'(recovery|resync|reshape|check)\s*=\s*([\d.]+)%')


class _MdModule(_Module):
    """Linux software RAID health from /proc/mdstat — the mdadm sibling of the
    zfs module, counting arrays that are clean, degraded or rebuilding."""

    key = 'md'

    def commands(self) -> List[Command]:
        return [Command("cat /proc/mdstat 2>&1")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0 and not stdout.strip():
            return CollectResult.failed(f"Failed to read /proc/mdstat: {stderr}")

        ok = degraded = 0
        recovering = False
        logs = []

        for m in _ARRAY_RE.finditer(stdout):
            dev = m.group(1)
            block = stdout[m.end():]
            next_blank = block.find('\n\n')
            block = block if next_blank < 0 else block[:next_blank]

            state = _STATE_RE.search(block)
            recov = _RECOVERY_RE.search(block)

            if state:
                expected, active, flags = int(state.group(1)), int(state.group(2)), state.group(3)
                if flags.count('_') > 0 or active < expected:
                    degraded += 1
                    logs.append((f"{dev}: DEGRADED [{active}/{expected}] [{flags}]", "ERROR"))
                    continue

            if recov:
                recovering = True
                logs.append((f"{dev}: {recov.group(1)} {recov.group(2)}% in progress", "WARNING"))
                ok += 1
                continue

            ok += 1
            logs.append((f"{dev}: clean", "INFO"))

        if ok + degraded == 0:
            return CollectResult(
                logs=[("No RAID arrays found in /proc/mdstat", "WARNING")], status='offline')

        metrics = {
            'arrays_total': float(ok + degraded),
            'arrays_ok': float(ok),
            'arrays_degraded': float(degraded),
        }
        if degraded:
            status = 'failed'
        elif recovering:
            status = 'warning'
        else:
            status = 'online'
        return CollectResult(metrics=metrics, logs=logs, status=status)

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'md_total_card': {'metric': 'arrays_total', 'title': 'ARRAYS', 'format': 'int'},
            'md_ok_card': {
                'metric': 'arrays_ok', 'title': 'CLEAN', 'format': 'int',
                'color': 'disks_always_online',
            },
            'md_degraded_card': {
                'metric': 'arrays_degraded', 'title': 'DEGRADED', 'format': 'int',
                'color': 'disks_nonzero_failed',
            },
        }

    def card_row(self) -> List[str]:
        return ['md_total_card', 'md_ok_card', 'md_degraded_card']


# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------

_SECTOR_BYTES = 512

_VIRTUAL_PREFIXES = ('loop', 'ram', 'dm-', 'sr', 'fd', 'md')


def _parse_diskstats(stdout: str) -> Dict[str, Tuple[int, int]]:
    """Map device name to (sectors read, sectors written) from one /proc/diskstats block."""
    result: Dict[str, Tuple[int, int]] = {}
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        name = fields[2]
        try:
            sectors_read = int(fields[5])
            sectors_written = int(fields[9])
        except (ValueError, IndexError):
            continue
        result[name] = (sectors_read, sectors_written)
    return result


def _is_physical(name: str) -> bool:
    """Whether a device is a whole physical disk rather than a partition or virtual node."""
    if any(name.startswith(p) for p in _VIRTUAL_PREFIXES):
        return False
    if name[-1:].isdigit() and not name.startswith('nvme') and not name.startswith('mmcblk'):
        return False
    if 'p' in name and name.split('p')[-1].isdigit() and (name.startswith('nvme') or name.startswith('mmcblk')):
        return False
    return True


def _auto_detect_device(s1: Dict[str, Tuple[int, int]], s2: Dict[str, Tuple[int, int]]) -> Optional[str]:
    """The busiest physical disk across the two snapshots, or the first one when all are idle."""
    activity = {}
    for name in s1:
        if name not in s2 or not _is_physical(name):
            continue
        r = (s2[name][0] - s1[name][0]) + (s2[name][1] - s1[name][1])
        activity[name] = r
    if not activity:
        physical = [n for n in s1 if _is_physical(n)]
        return physical[0] if physical else None
    return max(activity, key=activity.__getitem__)


def _format_rate(kbps: float) -> str:
    """A KB/s rate rendered as a human-readable throughput string."""
    if kbps >= 1024:
        return f"{kbps / 1024:.1f} MB/s"
    return f"{kbps:.1f} KB/s"


class _IoModule(_Module):
    """Read and write throughput for one disk, from two /proc/diskstats samples
    a second apart taken on the target so the sleep costs one round trip."""

    key = 'io'

    def __init__(self, plugin: 'Disks', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.device: Optional[str] = options.get('device')

    @property
    def setting_key(self) -> str:
        return f"disks:{self.plugin.id}:active_device"

    def commands(self) -> List[Command]:
        return [Command("cat /proc/diskstats && sleep 1 && echo '---SNAP---' && cat /proc/diskstats")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/diskstats: {stderr}")

        halves = stdout.split('---SNAP---')
        if len(halves) < 2:
            return CollectResult.failed("Unexpected /proc/diskstats output format")

        s1 = _parse_diskstats(halves[0])
        s2 = _parse_diskstats(halves[1])

        device = self.device or _auto_detect_device(s1, s2)
        if not device:
            return CollectResult.failed("No usable disk device found")

        if device not in s1 or device not in s2:
            return CollectResult.failed(f"Device '{device}' not found in /proc/diskstats")

        read_kbps = max(0.0, (s2[device][0] - s1[device][0]) * _SECTOR_BYTES / 1024)
        write_kbps = max(0.0, (s2[device][1] - s1[device][1]) * _SECTOR_BYTES / 1024)

        return CollectResult(
            metrics={'read_kbps': read_kbps, 'write_kbps': write_kbps},
            logs=[(
                f"Disk {device}: read {_format_rate(read_kbps)}, write {_format_rate(write_kbps)}",
                "INFO",
            )],
            status='online',
            settings={self.setting_key: device},
        )

    @property
    def active_device_text(self) -> str:
        return self.plugin.data.get_setting(self.setting_key) or self.device or 'Detecting...'

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'io_device_card': {'title': 'DEVICE', 'value_attr': '_io_device', 'refresh': True},
            'read_card': {'metric': 'read_kbps', 'title': 'READ', 'format': 'kbps_rate'},
            'write_card': {'metric': 'write_kbps', 'title': 'WRITE', 'format': 'kbps_rate'},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {
            'read_chart': {'metric': 'read_kbps', 'title': 'READ THROUGHPUT (KB/s)'},
            'write_chart': {'metric': 'write_kbps', 'title': 'WRITE THROUGHPUT (KB/s)'},
        }


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

# Canonical order; also the set of names `modules` may enable.
_MODULE_TYPES = [_SmartModule, _ZfsModule, _MdModule, _IoModule]


class Disks(ModularPlugin):
    MODULE_TYPES = _MODULE_TYPES
    MODULE_LABEL = 'disks'
    # smart needs smartctl and privileges, zfs needs a pool; both are opt-in so
    # that a bare monitor works on a VM with neither.
    DEFAULT_MODULES = ('io',)

    @property
    def _io_device(self) -> str:
        module = self._module('io')
        return module.active_device_text if module else '--'
from vigil.core.ui.spec import register_color_rule


@register_color_rule('disks_always_online')
def _always_online(v):
    return None if v is None else 'online'


@register_color_rule('disks_nonzero_failed')
def _nonzero_failed(v):
    if v is None:
        return None
    return 'failed' if v else 'online'



def _module_options(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Resolve this plugin's `modules` block to {module key: options}."""
    return module_options('disks', config, _MODULE_TYPES,
                          Disks.DEFAULT_MODULES)
