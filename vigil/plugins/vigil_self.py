import os
import time
from typing import Any, Dict, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
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

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.memory_warning   = float(config.get('memory_warning',   256))
        self.memory_threshold = float(config.get('memory_threshold', 512))
        self.stale_warning    = float(config.get('stale_warning',     3))
        self.stale_threshold  = float(config.get('stale_threshold',  10))
        self._started_at = time.time()
        self._last_cpu_sample: Optional[Tuple[float, float]] = None
        self.target = 'vigil'

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
            if not p._last_collected:
                continue
            overdue = (now - p._last_collected) / p.interval if p.interval else 0
            if overdue >= self.stale_threshold:
                stalled += 1
                offenders.append((overdue, p.name))
            elif overdue >= self.stale_warning:
                late += 1
                offenders.append((overdue, p.name))

        offenders.sort(reverse=True)
        return total, late, stalled, offenders[:5]

    async def on_collect(self):
        rss_mb = _read_rss_mb()
        cpu_seconds = _read_cpu_seconds()
        uptime_seconds = time.time() - self._started_at

        self.db_metrics.metric('uptime_seconds', uptime_seconds)
        if rss_mb is not None:
            self.db_metrics.metric('memory_mb', rss_mb)

        cpu_pct = None
        if cpu_seconds is not None:
            now = time.monotonic()
            if self._last_cpu_sample:
                prev_time, prev_cpu = self._last_cpu_sample
                elapsed = now - prev_time
                if elapsed > 0:
                    cpu_pct = 100.0 * (cpu_seconds - prev_cpu) / elapsed
                    self.db_metrics.metric('cpu_pct', cpu_pct)
            self._last_cpu_sample = (now, cpu_seconds)

        total, late, stalled, offenders = self._collection_health()
        self.db_metrics.metric('monitors_total', float(total))
        self.db_metrics.metric('monitors_late', float(late))
        self.db_metrics.metric('monitors_stalled', float(stalled))

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
        self.db_logger.write(' | '.join(parts), level=log_level)
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class VigilSelfUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        memory_warning   = float(self.config.get('memory_warning',   256))
        memory_threshold = float(self.config.get('memory_threshold', 512))

        page = self.page(metric_names=[
            'uptime_seconds', 'memory_mb', 'monitors_total',
            'monitors_late', 'monitors_stalled',
        ])

        def _uptime_or_dash(v):
            return '--' if v is None else _format_uptime(v)

        def _memory_or_dash(v):
            return '-- MB' if v is None else f'{v:.0f} MB'

        def _monitors_text(_):
            total = page.model.metrics.get('monitors_total')
            if total is None:
                return '--'
            n_late = int(page.model.metrics.get('monitors_late') or 0)
            n_stalled = int(page.model.metrics.get('monitors_stalled') or 0)
            if n_stalled:
                return f'{int(total)} ({n_stalled} stalled)'
            if n_late:
                return f'{int(total)} ({n_late} late)'
            return f'{int(total)} OK'

        with layout.cell('uptime_card'):
            info_card('VIGIL UPTIME', '--').bind_text_from(
                page.model, ('metrics', 'uptime_seconds'), backward=_uptime_or_dash)
        with layout.cell('memory_card'):
            memory_label = info_card('MEMORY', '-- MB').bind_text_from(
                page.model, ('metrics', 'memory_mb'), backward=_memory_or_dash)
        with layout.cell('monitors_card'):
            monitors_label = info_card('MONITORS', '--').bind_text_from(
                page.model, ('metrics', 'monitors_total'), backward=_monitors_text)
        with layout.cell('chart'):
            history_chart(page, 'VIGIL MEMORY (MB)', self.id, 'memory_mb')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_colors():
            memory = page.model.metrics.get('memory_mb')
            if memory is not None:
                memory_label.style(
                    f'color: {STATUS_COLORS[_level_for(memory, memory_warning, memory_threshold)]}'
                )

            total = page.model.metrics.get('monitors_total')
            if total is not None:
                n_late = int(page.model.metrics.get('monitors_late') or 0)
                n_stalled = int(page.model.metrics.get('monitors_stalled') or 0)
                if n_stalled:
                    monitors_label.style(f'color: {STATUS_COLORS["failed"]}')
                elif n_late:
                    monitors_label.style(f'color: {STATUS_COLORS["warning"]}')
                else:
                    monitors_label.style(f'color: {STATUS_COLORS["online"]}')

        page.on_refresh(update_colors)

        page.start()


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
