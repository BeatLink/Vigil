"""Load average, read from /proc/loadavg and scaled by core count."""

from typing import Any, Dict, List

from vigil.plugins.base.signal_plugin import (
    LOG_LEVEL as _LOG_LEVEL, SignalPlugin,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig
from vigil.plugins.base.plugin_helpers import level_for as _level_for


class Load(SignalPlugin):
    """Load average from /proc/loadavg, normalized by core count so 100%
    means the host is exactly at capacity. Thresholds are optional; without
    both, load is collected and charted but never changes the status."""

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.warning   = float(config['warning'])   if 'warning'   in config else None
        self.threshold = float(config['threshold']) if 'threshold' in config else None

        self._color_rule = None
        if self.warning is not None and self.threshold is not None:
            from vigil.core.ui.spec import register_color_rule, threshold_color
            self._color_rule = f'load_{self.id}'
            register_color_rule(self._color_rule)(
                threshold_color(warning=self.warning, threshold=self.threshold))

    SAMPLED = True

    def commands(self) -> List[Command]:
        return [Command('echo "LOAD:$(cat /proc/loadavg)"; echo "CPUS:$(nproc)"')]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Load collection failed: {stderr}")

        lines = stdout.splitlines()
        load_line = next((l for l in lines if l.startswith('LOAD:')), None)
        cpus_line = next((l for l in lines if l.startswith('CPUS:')), None)
        if not load_line:
            return CollectResult.failed(f"Incomplete load output: {stdout!r}")

        try:
            cpu_count    = max(1, int(cpus_line.removeprefix('CPUS:').strip())) if cpus_line else 1
            parts        = load_line.removeprefix('LOAD:').split()
            load_pct_1m  = float(parts[0]) / cpu_count * 100.0
            load_pct_5m  = float(parts[1]) / cpu_count * 100.0
            load_pct_15m = float(parts[2]) / cpu_count * 100.0
        except (ValueError, IndexError) as e:
            return CollectResult.failed(f"Failed to parse load output: {e}")

        if self.warning is not None and self.threshold is not None:
            status = _level_for(load_pct_1m, self.warning, self.threshold)
        else:
            status = 'online'

        return CollectResult(
            metrics={
                'load_pct_1m':  load_pct_1m,
                'load_pct_5m':  load_pct_5m,
                'load_pct_15m': load_pct_15m,
            },
            logs=[(
                f"LOAD {load_pct_1m:.0f}% / {load_pct_5m:.0f}% / {load_pct_15m:.0f}% (1m/5m/15m, "
                f"{cpu_count} cores)",
                _LOG_LEVEL[status],
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        load_1m_card = {'metric': 'load_pct_1m', 'title': 'LOAD 1M', 'format': 'percent0_plain_dash'}
        if self._color_rule:
            load_1m_card['color'] = self._color_rule
        return {
            'load_1m_card':  load_1m_card,
            'load_5m_card':  {'metric': 'load_pct_5m',  'title': 'LOAD 5M',  'format': 'percent0_plain_dash'},
            'load_15m_card': {'metric': 'load_pct_15m', 'title': 'LOAD 15M', 'format': 'percent0_plain_dash'},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'load_chart': {'metric': 'load_pct_1m', 'title': 'LOAD AVERAGE (%)'}}
