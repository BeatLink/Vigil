"""
Self-monitoring: reports on the Vigil process itself.

Every other plugin answers "is that host healthy?". This one answers "is the
thing asking that question still working?" — the gap that makes a dashboard
dangerous rather than merely incomplete. When the engine wedges, monitors stop
updating and the UI keeps serving the last known statuses, so a screen full of
green is indistinguishable from a screen that stopped being written to. A
monitor that goes stale *silently* is worse than no monitor.

Runs in-process rather than over SSH: it measures this interpreter, so there is
nothing to connect to. `ssh_config` is therefore not used, and none of the
inherited SSH machinery is touched.

The engine reference arrives via the `engine` class attribute, set once by
VigilEngine at startup. Plugins are constructed with (name, config, db) and
nothing else, and widening that signature for the single plugin that needs it
would put an unused parameter on all twenty-eight.

Config options:
  memory_warning    Process RSS in MB that triggers warning (default: 256)
  memory_threshold  Process RSS in MB that triggers failed  (default: 512)
  stale_warning     Multiple of a monitor's interval before its last collection
                    counts as late    (default: 3)
  stale_threshold   Multiple of a monitor's interval before it counts as stalled
                    (default: 10)
"""
import os
import time
from typing import Any, Dict, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for

# Ticks per second for /proc/self/stat's utime+stime fields. This is a kernel
# build constant (USER_HZ), fixed at 100 on every Linux platform Vigil targets,
# and os.sysconf is the supported way to read it.
_CLOCK_TICKS = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100

_DEFAULT_LAYOUT = [
    ['uptime_card', 'memory_card', 'monitors_card'],
    ['chart'],
    ['events'],
]


def _read_rss_mb() -> Optional[float]:
    """
    Resident set size of this process, in MB, or None if unreadable.

    Reads VmRSS from /proc/self/status rather than using psutil: Vigil ships
    four runtime dependencies and self-monitoring is not a good reason for a
    fifth, least of all in a tool whose pitch is being lightweight.
    """
    try:
        with open('/proc/self/status') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    # "VmRSS:      12345 kB"
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read_cpu_seconds() -> Optional[float]:
    """CPU seconds (user + system) consumed by this process, or None."""
    try:
        with open('/proc/self/stat') as fh:
            fields = fh.read().split()
        # Fields 14 and 15 (1-indexed) are utime and stime. The comm field can
        # itself contain spaces, but it is parenthesised and precedes them, so
        # splitting from the right of the closing paren is what makes indexing
        # safe here — the process name is "python3", so a plain split is fine,
        # but this stays correct if that ever changes.
        return (int(fields[13]) + int(fields[14])) / _CLOCK_TICKS
    except (OSError, ValueError, IndexError):
        return None


class VigilSelfCollectorPlugin(CollectorPlugin):
    """Monitors Vigil's own process health and collection liveness."""

    # Set by VigilEngine at startup so this plugin can inspect the monitor
    # tree. None when Vigil runs without an engine (tests, direct use).
    engine: Any = None

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.memory_warning   = float(config.get('memory_warning',   256))
        self.memory_threshold = float(config.get('memory_threshold', 512))
        self.stale_warning    = float(config.get('stale_warning',     3))
        self.stale_threshold  = float(config.get('stale_threshold',  10))
        # Wall-clock start, for reporting uptime.
        self._started_at = time.time()
        # Previous (monotonic, cpu_seconds) sample, for computing CPU percent
        # across the interval. The first collection has nothing to diff
        # against, so it reports no CPU figure rather than a meaningless one.
        self._last_cpu_sample: Optional[Tuple[float, float]] = None
        # Self-monitoring reports on this process, not a remote host.
        self.target = 'vigil'

    def _walk_monitors(self):
        """Yield every monitor in the tree except this one."""
        if self.engine is None:
            return
        stack = list(self.engine.plugins)
        while stack:
            p = stack.pop()
            stack.extend(p.children)
            if p is not self:
                yield p

    def _collection_health(self) -> Tuple[int, int, int, list]:
        """
        Inspect every monitor's last collection time.

        Returns (total, late, stalled, worst_offenders). A monitor is late once
        it has gone `stale_warning` intervals without collecting and stalled at
        `stale_threshold` — measured in multiples of each monitor's own interval
        so a 30s uptime check and a 1h backup check are judged on their own
        terms rather than against one global deadline.
        """
        now = time.monotonic()
        total = late = stalled = 0
        offenders = []

        for p in self._walk_monitors():
            # Groups aggregate their children's status and do no collection of
            # their own, so they have no meaningful staleness.
            if p.children:
                continue
            total += 1
            # Never collected yet: not evidence of a stall. A monitor with a
            # long interval legitimately sits unrun until its first poll is due.
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

        # CPU as a percentage of one core across the interval just elapsed.
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

        # Status is the worst of the two independent signals: a stalled
        # collection loop and a process eating memory are both failures, and
        # neither should be able to mask the other.
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
    """Dashboard rendering for Vigil's self-monitor. See
    VigilSelfCollectorPlugin for collection logic."""

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart, on_data_event
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        # UIPlugin has no memory_warning/memory_threshold attributes (that's
        # collector-side state) — re-derived here from config the same way
        # VigilSelfCollectorPlugin.__init__ does.
        memory_warning   = float(self.config.get('memory_warning',   256))
        memory_threshold = float(self.config.get('memory_threshold', 512))

        with layout.cell('uptime_card'):
            uptime_label = info_card('VIGIL UPTIME', '--')
        with layout.cell('memory_card'):
            memory_label = info_card('MEMORY', '-- MB')
        with layout.cell('monitors_card'):
            monitors_label = info_card('MONITORS', '--')
        with layout.cell('chart'):
            history_chart('VIGIL MEMORY (MB)', self.id, 'memory_mb')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            uptime = self.latest_metric('uptime_seconds')
            memory = self.latest_metric('memory_mb')
            total   = self.latest_metric('monitors_total')
            late    = self.latest_metric('monitors_late')
            stalled = self.latest_metric('monitors_stalled')

            if uptime:
                uptime_label.text = _format_uptime(uptime.value)
            if memory:
                memory_label.text = f'{memory.value:.0f} MB'
                memory_label.style(
                    f'color: {STATUS_COLORS[_level_for(memory.value, memory_warning, memory_threshold)]}'
                )
            if total:
                n_late    = int(late.value)    if late    else 0
                n_stalled = int(stalled.value) if stalled else 0
                if n_stalled:
                    monitors_label.text = f'{int(total.value)} ({n_stalled} stalled)'
                    monitors_label.style(f'color: {STATUS_COLORS["failed"]}')
                elif n_late:
                    monitors_label.text = f'{int(total.value)} ({n_late} late)'
                    monitors_label.style(f'color: {STATUS_COLORS["warning"]}')
                else:
                    monitors_label.text = f'{int(total.value)} OK'
                    monitors_label.style(f'color: {STATUS_COLORS["online"]}')

        on_data_event('metric', uptime_label, update_cards)


def _format_uptime(seconds: float) -> str:
    """Format a duration as a compact human-readable string (e.g. '3d 4h')."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
