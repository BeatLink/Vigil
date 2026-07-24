from typing import Any, Dict, List, Optional

from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult
from vigil.plugins.base.web_plugin_base import UIPlugin


def _extract_counter(block: str, key: str) -> Optional[int]:
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
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.alert_for  = int(config.get('alert_for', 3))
        self.is_warning = bool(config.get('is_warning', False))
        self._last_total: Optional[int] = None
        self._since_kill: Optional[int] = None

    def commands(self) -> List[Command]:
        return [Command("cat /proc/vmstat")]

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
                status='warning' if self.is_warning else 'failed',
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


class OomUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.is_warning = bool(config.get('is_warning', False))

        from vigil.core.ui.ui.spec import register_color_rule
        self._color_rule_name = f'oom_recent_{self.id}'

        @register_color_rule(self._color_rule_name)
        def _recent_color(v, _is_warning=self.is_warning):
            if v is None:
                return None
            return 'online' if v == 0 else ('warning' if _is_warning else 'failed')

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'total_card': {
                    'metric': 'oom_kills_total', 'title': 'OOM KILLS (BOOT)',
                    'format': 'count_comma_rounded',
                },
                'recent_card': {
                    'metric': 'oom_kills_new', 'title': 'SINCE LAST CHECK',
                    'format': 'count_comma_rounded', 'color': self._color_rule_name,
                },
            },
            'chart': {'metric': 'oom_kills_total', 'title': 'OOM KILLS SINCE BOOT'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.ui.spec import generic_render
        generic_render(self, context)
