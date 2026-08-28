"""Kernel OOM kills, counted from /proc/vmstat and followed in the journal."""

from typing import Any, Dict, List, Optional

from vigil.plugins.base.signal_plugin import (
    SignalPlugin,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig
from vigil_agent.protocol import StreamSpec


def _extract_counter(block: str, key: str) -> Optional[int]:
    for line in block.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == key:
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


class Oom(SignalPlugin):
    """Kernel OOM kills from /proc/vmstat's oom_kill counter, plus — on an
    agent-backed host — the kernel journal line the OOM killer itself emits.
    The counter remains the authority on totals; the journal only makes a kill
    visible immediately and carries the process name, which the counter can't."""

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.alert_for  = int(config.get('alert_for', 3))
        self.is_warning = bool(config.get('is_warning', False))
        self._last_total: Optional[int] = None
        self._since_kill: Optional[int] = None

        from vigil.core.ui.spec import register_color_rule
        self._color_rule = f'oom_recent_{self.id}'

        @register_color_rule(self._color_rule)
        def _recent_color(v, _is_warning=self.is_warning):
            if v is None:
                return None
            return 'online' if v == 0 else ('warning' if _is_warning else 'failed')

    @property
    def _kill_status(self) -> str:
        return 'warning' if self.is_warning else 'failed'

    SAMPLED = True

    def subscriptions(self) -> List[StreamSpec]:
        """The counter sample alongside the kernel journal: the sample carries
        the vmstat total, the journal names the process a kill took."""
        return super().subscriptions() + [StreamSpec(
            id=f'{self.id}:journal',
            kind='journal',
            params={'kernel': True, 'grep': 'Out of memory'},
        )]

    def parse_event(self, stream_id: str, payload: Dict[str, Any],
                    timestamp: float) -> Optional[CollectResult]:
        if not stream_id.endswith(':journal'):
            return super().parse_event(stream_id, payload, timestamp)
        message = str(payload.get('message', '')).strip()
        if not message:
            return None
        self._since_kill = 0
        return CollectResult(
            logs=[(f"OOM killer fired: {message}", "ERROR")],
            status=self._kill_status,
        )

    def commands(self) -> List[Command]:
        return [Command('cat /proc/vmstat')]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/vmstat: {stderr}")

        total = _extract_counter(stdout, 'oom_kill')
        if total is None:
            return CollectResult.failed(
                "No 'oom_kill' counter in /proc/vmstat (kernel too old?)",
                level="WARNING", status='offline')

        metrics = {'oom_kills_total': float(total)}
        previous, self._last_total = self._last_total, total

        if previous is None:
            return CollectResult(
                metrics=metrics,
                logs=[(f"Baseline established: {total} OOM kill(s) since boot", "INFO")],
                status='online',
            )

        if total < previous:
            return CollectResult(
                metrics=metrics,
                logs=[(f"OOM counter reset ({previous} -> {total}); host likely rebooted", "INFO")],
                status='online',
            )

        delta = total - previous
        metrics['oom_kills_new'] = float(delta)

        if delta > 0:
            self._since_kill = 0
            return CollectResult(
                metrics=metrics,
                logs=[(
                    f"{delta} OOM kill(s) since last check — the kernel terminated "
                    f"process(es) to reclaim memory ({total} total since boot)",
                    "WARNING" if self.is_warning else "ERROR",
                )],
                status=self._kill_status,
            )

        if self._since_kill is not None:
            self._since_kill += 1
            if self._since_kill < self.alert_for:
                return CollectResult(
                    metrics=metrics,
                    logs=[(
                        f"No new OOM kills ({self._since_kill}/{self.alert_for} "
                        f"collections since the last one)",
                        "WARNING",
                    )],
                    status='warning',
                )
            self._since_kill = None

        return CollectResult(
            metrics=metrics,
            logs=[(f"No OOM kills ({total} total since boot)", "INFO")],
            status='online',
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'oom_total_card': {'metric': 'oom_kills_total', 'title': 'OOM KILLS (BOOT)',
                               'format': 'count_comma_rounded'},
            'oom_recent_card': {'metric': 'oom_kills_new', 'title': 'OOM SINCE LAST CHECK',
                                'format': 'count_comma_rounded', 'color': self._color_rule},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'oom_chart': {'metric': 'oom_kills_total', 'title': 'OOM KILLS SINCE BOOT'}}
