import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for

_CLOCK_TICKS = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100

_DEFAULT_LAYOUT = [
    ['uptime_card', 'memory_card', 'monitors_card'],
    ['chart'],
    ['events'],
]


def _read_rss_mb() -> Optional[float]:
    try:
        with open('/proc/self/status') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read_cpu_seconds() -> Optional[float]:
    try:
        with open('/proc/self/stat') as fh:
            fields = fh.read().split()
        return (int(fields[13]) + int(fields[14])) / _CLOCK_TICKS
    except (OSError, ValueError, IndexError):
        return None


class VigilSelfCollectorPlugin(CollectorPlugin):
    engine: Any = None

    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.memory_warning   = float(config.get('memory_warning',   256))
        self.memory_threshold = float(config.get('memory_threshold', 512))
        self.stale_warning    = float(config.get('stale_warning',     3))
        self.stale_threshold  = float(config.get('stale_threshold',  10))
        self._started_at = time.time()
        self._last_cpu_sample: Optional[Tuple[float, float]] = None
        self.target = 'vigil'

    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def _walk_monitors(self):
        if self.engine is None:
            return
        stack = list(self.engine.plugins)
        while stack:
            p = stack.pop()
            stack.extend(p.children)
            if p is not self:
                yield p

    def _collection_health(self) -> Tuple[int, int, int, list]:
        now = time.monotonic()
        total = late = stalled = 0
        offenders = []

        for p in self._walk_monitors():
            if p.children:
                continue
            total += 1
            last = self.engine._last_collected.get(p.id, 0.0) if self.engine else 0.0
            if not last:
                continue
            overdue = (now - last) / p.interval if p.interval else 0
            if overdue >= self.stale_threshold:
                stalled += 1
                offenders.append((overdue, p.name))
            elif overdue >= self.stale_warning:
                late += 1
                offenders.append((overdue, p.name))

        offenders.sort(reverse=True)
        return total, late, stalled, offenders[:5]

    def local_call(self) -> Optional[Callable[[], Any]]:
        def _sample():
            rss_mb = _read_rss_mb()
            cpu_seconds = _read_cpu_seconds()
            uptime_seconds = time.time() - self._started_at

            cpu_pct = None
            if cpu_seconds is not None:
                now = time.monotonic()
                if self._last_cpu_sample:
                    prev_time, prev_cpu = self._last_cpu_sample
                    elapsed = now - prev_time
                    if elapsed > 0:
                        cpu_pct = 100.0 * (cpu_seconds - prev_cpu) / elapsed
                self._last_cpu_sample = (now, cpu_seconds)

            total, late, stalled, offenders = self._collection_health()
            return {
                'rss_mb': rss_mb,
                'cpu_pct': cpu_pct,
                'uptime_seconds': uptime_seconds,
                'total': total,
                'late': late,
                'stalled': stalled,
                'offenders': offenders,
            }
        return _sample

    def parse_local(self, result: Any) -> CollectResult:
        rss_mb = result['rss_mb']
        cpu_pct = result['cpu_pct']
        uptime_seconds = result['uptime_seconds']
        total, late, stalled, offenders = result['total'], result['late'], result['stalled'], result['offenders']

        metrics = {'uptime_seconds': uptime_seconds, 'monitors_total': float(total),
                   'monitors_late': float(late), 'monitors_stalled': float(stalled)}
        if rss_mb is not None:
            metrics['memory_mb'] = rss_mb
        if cpu_pct is not None:
            metrics['cpu_pct'] = cpu_pct

        mem_level = (
            _level_for(rss_mb, self.memory_warning, self.memory_threshold)
            if rss_mb is not None else 'online'
        )
        if stalled:
            collect_level = 'failed'
        elif late:
            collect_level = 'warning'
        else:
            collect_level = 'online'

        overall = max(
            (mem_level, collect_level),
            key=lambda lvl: ('online', 'warning', 'failed').index(lvl),
        )

        parts = [f"up {_format_uptime(uptime_seconds)}"]
        if rss_mb is not None:
            parts.append(f"RSS {rss_mb:.0f} MB")
        if cpu_pct is not None:
            parts.append(f"CPU {cpu_pct:.1f}%")
        parts.append(f"{total} monitors")
        if late or stalled:
            parts.append(f"{late} late, {stalled} stalled")
        if offenders:
            worst = ', '.join(f"{name} ({mult:.0f}x)" for mult, name in offenders)
            parts.append(f"worst: {worst}")

        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        return CollectResult(metrics=metrics, logs=[(' | '.join(parts), log_level)], status=overall)


class VigilSelfUIPlugin(UIPlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.memory_warning   = float(self.config.get('memory_warning',   256))
        self.memory_threshold = float(self.config.get('memory_threshold', 512))

        from vigil.web.ui.spec import register_color_rule, register_formatter, threshold_color, register_item_formatter
        self._memory_color_name = f'vigil_self_memory_{self.id}'
        register_color_rule(self._memory_color_name)(
            threshold_color(warning=self.memory_warning, threshold=self.memory_threshold))
        self._uptime_format_name = f'vigil_self_uptime_{self.id}'
        register_formatter(self._uptime_format_name)(lambda v: '--' if v is None else _format_uptime(v))
        self._memory_format_name = f'vigil_self_memory_fmt_{self.id}'
        register_formatter(self._memory_format_name)(lambda v: '-- MB' if v is None else f'{v:.0f} MB')
        self._monitors_format_name = f'vigil_self_monitors_{self.id}'
        register_item_formatter(self._monitors_format_name)(self._monitors_text)
        self._monitors_color_name = f'vigil_self_monitors_color_{self.id}'
        register_item_formatter(self._monitors_color_name)(self._monitors_color)

    @staticmethod
    def _monitors_text(values: Dict[str, Any]) -> str:
        total = values.get('monitors_total')
        if total is None:
            return '--'
        n_late = int(values.get('monitors_late') or 0)
        n_stalled = int(values.get('monitors_stalled') or 0)
        if n_stalled:
            return f'{int(total)} ({n_stalled} stalled)'
        if n_late:
            return f'{int(total)} ({n_late} late)'
        return f'{int(total)} OK'

    @staticmethod
    def _monitors_color(values: Dict[str, Any]) -> Optional[str]:
        if values.get('monitors_total') is None:
            return None
        if int(values.get('monitors_stalled') or 0):
            return 'failed'
        if int(values.get('monitors_late') or 0):
            return 'warning'
        return 'online'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'uptime_card': {'metric': 'uptime_seconds', 'title': 'VIGIL UPTIME',
                                'format': self._uptime_format_name},
                'memory_card': {'metric': 'memory_mb', 'title': 'MEMORY', 'format': self._memory_format_name,
                                'color': self._memory_color_name},
                'monitors_card': {'title': 'MONITORS',
                                  'metrics': ['monitors_total', 'monitors_late', 'monitors_stalled'],
                                  'format_fn': self._monitors_format_name, 'color_fn': self._monitors_color_name},
            },
            'chart': {'metric': 'memory_mb', 'title': 'VIGIL MEMORY (MB)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
