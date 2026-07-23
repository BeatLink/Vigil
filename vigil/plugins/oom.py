from typing import Dict, Any, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin


def _extract_counter(block: str, key: str) -> Optional[int]:
    """Return the value on the `key` line of a /proc/vmstat block.

    /proc/vmstat is `name value` per line. Returns None if the line is absent,
    which is normal on kernels that don't expose the counter at all.
    """
    for line in block.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == key:
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'recent_card'],
    ['oom_chart'],
    ['events'],
]


class OomCollectorPlugin(CollectorPlugin):
    """
    Detects kernel Out-Of-Memory kills over SSH via /proc/vmstat — no extra
    tools required on the target.

    An OOM kill is an *event*, not a level: the kernel reaps a process, memory
    drops back to normal, and an interval-sampled memory_usage percentage sees
    nothing wrong. `oom_kill` is a monotonic counter since boot, so comparing it
    against the previous collection catches kills that happened between polls,
    whichever process was the victim.

    The first collection after startup establishes a baseline and reports online
    (the counter is cumulative since boot, so a non-zero value on the first read
    says nothing about the present). Thereafter, any increase is reported once,
    then decays back to online after `alert_for` collections without a new kill.

    A counter that goes backwards means the host rebooted; that resets the
    baseline rather than reporting a negative delta.

    Config options:
      alert_for   Collections to keep alerting after a kill (default: 3)
      is_warning  Report kills as warning rather than failed (default: false)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.alert_for  = int(config.get('alert_for', 3))
        self.is_warning = bool(config.get('is_warning', False))
        self._last_total: Optional[int] = None
        self._since_kill: Optional[int] = None

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output("cat /proc/vmstat")
        if ret != 0:
            self.db_logger.write(f"Failed to read /proc/vmstat: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        total = _extract_counter(stdout, 'oom_kill')
        if total is None:
            # Pre-4.13 kernels lack oom_kill entirely — report offline rather than
            # healthy, so a silently unmonitored host is visible as such.
            self.db_logger.write(
                "No 'oom_kill' counter in /proc/vmstat (kernel too old?)", level="WARNING"
            )
            self.set_status('offline')
            return

        self.db_metrics.metric('oom_kills_total', float(total))

        previous, self._last_total = self._last_total, total

        if previous is None:
            self.db_logger.write(
                f"Baseline established: {total} OOM kill(s) since boot", level="INFO"
            )
            self.set_status('online')
            return

        if total < previous:
            # Counter went backwards: the host rebooted. Re-baseline silently.
            self.db_logger.write(
                f"OOM counter reset ({previous} -> {total}); host likely rebooted",
                level="INFO"
            )
            self.set_status('online')
            return

        delta = total - previous
        self.db_metrics.metric('oom_kills_new', float(delta))

        if delta > 0:
            self._since_kill = 0
            self.db_logger.write(
                f"{delta} OOM kill(s) since last check — the kernel terminated "
                f"process(es) to reclaim memory ({total} total since boot)",
                level="WARNING" if self.is_warning else "ERROR"
            )
            self.set_status('warning' if self.is_warning else 'failed')
            return

        # No new kills. Hold the alert for alert_for collections so a kill isn't
        # missed by anyone glancing at the dashboard between polls.
        if self._since_kill is not None:
            self._since_kill += 1
            if self._since_kill < self.alert_for:
                self.db_logger.write(
                    f"No new OOM kills ({self._since_kill}/{self.alert_for} "
                    f"collections since the last one)",
                    level="WARNING"
                )
                self.set_status('warning')
                return
            self._since_kill = None

        self.db_logger.write(f"No OOM kills ({total} total since boot)", level="INFO")
        self.set_status('online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class OomUIPlugin(UIPlugin):
    """Dashboard rendering for the oom monitor."""

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['oom_kills_total', 'oom_kills_new'])

        def _count_or_dash(v):
            return '--' if v is None else f'{v:,.0f}'

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('total_card'):
            info_card('OOM KILLS (BOOT)', '--').bind_text_from(
                page.model, ('metrics', 'oom_kills_total'), backward=_count_or_dash)
        with layout.cell('recent_card'):
            recent_label = info_card('SINCE LAST CHECK', '--').bind_text_from(
                page.model, ('metrics', 'oom_kills_new'), backward=_count_or_dash)
        with layout.cell('oom_chart'):
            history_chart(page, 'OOM KILLS SINCE BOOT', self.id, 'oom_kills_total')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_recent_color():
            recent = page.model.metrics.get('oom_kills_new')
            if recent is not None:
                level = 'online' if recent == 0 else (
                    'warning' if self.config.get('is_warning', False) else 'failed')
                recent_label.style(f'color: {STATUS_COLORS[level]}')

        page.on_refresh(update_recent_color)

        page.start()
