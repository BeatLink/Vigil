import re
from typing import Dict, Any, Optional
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.plugin_utils import level_for as _level_for
from vigil.core.ui.components import info_card, history_chart, safe_timer
from vigil.core.ui.theme import STATUS_COLORS

_DEFAULT_LAYOUT_METRIC = [
    ['host_card', 'exit_card', 'value_card'],
    ['chart'],
    ['logs'],
]

_DEFAULT_LAYOUT_PLAIN = [
    ['host_card', 'exit_card'],
    ['logs'],
]


class CommandPlugin(BasePlugin):
    """
    Runs an arbitrary command over SSH and derives status from it — the generic
    escape hatch for checks that don't have a dedicated plugin.

    Status is determined in this order:
      1. If a `pattern` (regex with one capture group) is set, the first match
         in stdout is parsed as a float and compared against warning/threshold
         (same semantics as the numeric plugins). The captured value is stored
         as the `value` metric and charted.
      2. Otherwise status is driven by the command's exit code: 0 => online,
         non-zero => failed (or warning, if `nonzero_is_warning: true`).

    A per-cycle timeout wraps the command so a hung target can't stall the loop.
    Every run's stdout/stderr and exit code are logged for diagnosis.

    Config options:
      command            Shell command to run on the target        (required)
      timeout            Per-run timeout in seconds                 (default: 30)
      pattern            Regex with one capture group extracting a number (optional)
      warning            Value that triggers warning (needs pattern)      (optional)
      threshold          Value that triggers failed  (needs pattern)      (optional)
      invert             If true, values BELOW warning/threshold are bad  (default: false)
      nonzero_is_warning Treat non-zero exit as warning not failed  (default: false)
      value_label        Card label for the extracted value         (default: "VALUE")
      value_unit         Suffix appended to the value in the UI      (default: "")
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.command = config.get('command')
        self.timeout = int(config.get('timeout', 30))
        pattern = config.get('pattern')
        self.pattern = re.compile(pattern) if pattern else None
        self.warning   = config.get('warning')
        self.threshold = config.get('threshold')
        self.invert = bool(config.get('invert', False))
        self.nonzero_is_warning = bool(config.get('nonzero_is_warning', False))
        self.value_label = config.get('value_label', 'VALUE')
        self.value_unit = config.get('value_unit', '')

    def _level_for_value(self, value: float) -> str:
        """Map a value to a status level, honoring the `invert` flag."""
        if self.warning is None or self.threshold is None:
            return 'online'
        if self.invert:
            # Lower is worse: flip comparisons by negating both value and bounds
            return _level_for(-value, -float(self.warning), -float(self.threshold))
        return _level_for(value, float(self.warning), float(self.threshold))

    async def on_collect(self):
        if not self.command:
            self.db_logger.write("No 'command' configured", level="ERROR")
            self.set_status('failed')
            return

        # Wrap in `timeout` so a hung command can't block the polling loop.
        wrapped = f"timeout {self.timeout} sh -c {_shquote(self.command)}"
        ret, stdout, stderr = await self.ssh_collector.fetch_output(wrapped)

        # `timeout` exits 124 when it kills the command.
        if ret == 124:
            self.db_logger.write(f"Command timed out after {self.timeout}s", level="ERROR")
            self.set_status('failed')
            return

        self.db_metrics.metric('exit_code', float(ret))
        out_snippet = (stdout or stderr).strip()
        self.db_logger.write(
            f"exit={ret} {out_snippet[:200]}" if out_snippet else f"exit={ret}",
            level="INFO" if ret == 0 else "ERROR"
        )

        # Pattern mode: extract and threshold a numeric value.
        if self.pattern is not None:
            value = self._extract_value(stdout)
            if value is None:
                self.db_logger.write(
                    f"Pattern {self.pattern.pattern!r} did not match a number in output", level="ERROR"
                )
                self.set_status('failed')
                return
            self.db_metrics.metric('value', value)
            overall = self._level_for_value(value)
            log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
            self.db_logger.write(f"{self.value_label}: {value}{self.value_unit} -> {overall}", level=log_level)
            self.set_status(overall)
            return

        # Exit-code mode.
        if ret == 0:
            self.set_status('online')
        else:
            self.set_status('warning' if self.nonzero_is_warning else 'failed')

    def _extract_value(self, stdout: str) -> Optional[float]:
        m = self.pattern.search(stdout or '')
        if not m:
            return None
        # Prefer the first capture group; fall back to the whole match.
        raw = m.group(1) if m.groups() else m.group(0)
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        has_value = self.pattern is not None
        base = _DEFAULT_LAYOUT_METRIC if has_value else _DEFAULT_LAYOUT_PLAIN
        layout = PluginLayout(self.config, base if context == 'page' else make_inline_layout(base))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('exit_card'):
            exit_label = info_card('EXIT CODE', '--')
        if has_value:
            with layout.cell('value_card'):
                value_label = info_card(self.value_label, '--')
            with layout.cell('chart'):
                history_chart(self.value_label, self.name, 'value')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update():
            exit_m = self.latest_metric('exit_code')
            if exit_m is not None:
                code = int(exit_m.value)
                exit_label.text = str(code)
                exit_label.style(f"color: {STATUS_COLORS['online' if code == 0 else 'failed']}")
            if has_value:
                val_m = self.latest_metric('value')
                if val_m is not None:
                    value_label.text = f'{val_m.value:g}{self.value_unit}'
                    value_label.style(f"color: {STATUS_COLORS[self._level_for_value(val_m.value)]}")

        update()
        safe_timer(5.0, update)


def _shquote(s: str) -> str:
    """Single-quote a string for safe embedding inside a shell command."""
    return "'" + s.replace("'", "'\\''") + "'"
