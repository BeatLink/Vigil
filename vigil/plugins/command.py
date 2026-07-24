import re
from typing import Dict, Any, List, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for

_DEFAULT_LAYOUT_METRIC = [
    ['host_card', 'exit_card', 'value_card'],
    ['chart'],
    ['events'],
]

_DEFAULT_LAYOUT_PLAIN = [
    ['host_card', 'exit_card'],
    ['events'],
]


class CommandCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.command = config.get('command')
        self.command_timeout = int(config.get('timeout', 30))
        pattern = config.get('pattern')
        self.pattern = re.compile(pattern) if pattern else None
        self.warning   = config.get('warning')
        self.threshold = config.get('threshold')
        self.invert = bool(config.get('invert', False))
        self.nonzero_is_warning = bool(config.get('nonzero_is_warning', False))
        self.value_label = config.get('value_label', 'VALUE')
        self.value_unit = config.get('value_unit', '')

    def _level_for_value(self, value: float) -> str:
        if self.warning is None or self.threshold is None:
            return 'online'
        if self.invert:
            return _level_for(-value, -float(self.warning), -float(self.threshold))
        return _level_for(value, float(self.warning), float(self.threshold))

    def commands(self) -> List[Command]:
        if not self.command:
            return []
        wrapped = f"timeout {self.command_timeout} sh -c {_shquote(self.command)}"
        return [Command(wrapped)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        if not self.command:
            return CollectResult.failed("No 'command' configured")

        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr

        if ret == 124:
            return CollectResult.failed(f"Command timed out after {self.command_timeout}s")

        metrics = {'exit_code': float(ret)}
        out_snippet = (stdout or stderr).strip()
        logs = [(
            f"exit={ret} {out_snippet[:200]}" if out_snippet else f"exit={ret}",
            "INFO" if ret == 0 else "ERROR",
        )]

        if self.pattern is not None:
            value = self._extract_value(stdout)
            if value is None:
                logs.append((
                    f"Pattern {self.pattern.pattern!r} did not match a number in output", "ERROR"
                ))
                return CollectResult(metrics=metrics, logs=logs, status='failed')
            metrics['value'] = value
            overall = self._level_for_value(value)
            log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
            logs.append((f"{self.value_label}: {value}{self.value_unit} -> {overall}", log_level))
            return CollectResult(metrics=metrics, logs=logs, status=overall)

        if ret == 0:
            status = 'online'
        else:
            status = 'warning' if self.nonzero_is_warning else 'failed'
        return CollectResult(metrics=metrics, logs=logs, status=status)

    def _extract_value(self, stdout: str) -> Optional[float]:
        m = self.pattern.search(stdout or '')
        if not m:
            return None
        raw = m.group(1) if m.groups() else m.group(0)
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None


class CommandUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.theme import STATUS_COLORS

        pattern = self.config.get('pattern')
        warning = self.config.get('warning')
        threshold = self.config.get('threshold')
        invert = bool(self.config.get('invert', False))
        value_label = self.config.get('value_label', 'VALUE')
        value_unit = self.config.get('value_unit', '')

        def level_for_value(value: float) -> str:
            if warning is None or threshold is None:
                return 'online'
            if invert:
                return _level_for(-value, -float(warning), -float(threshold))
            return _level_for(value, float(warning), float(threshold))

        from vigil.web.ui.spec import FORMATTERS
        has_value = pattern is not None
        base = _DEFAULT_LAYOUT_METRIC if has_value else _DEFAULT_LAYOUT_PLAIN
        layout = PluginLayout(self.config, base if context == 'page' else make_inline_layout(base))
        metric_names = ['exit_code'] + (['value'] if has_value else [])
        page = self.ui.page(metric_names=metric_names)

        _exit_text = FORMATTERS['int']

        def _value_text(v):
            return '--' if v is None else f'{v:g}{value_unit}'

        with layout.cell('host_card'):
            self.ui.host_card()
        with layout.cell('exit_card'):
            exit_label = info_card('EXIT CODE', '--').bind_text_from(
                page.model, ('metrics', 'exit_code'), backward=_exit_text)
        if has_value:
            with layout.cell('value_card'):
                value_label_widget = info_card(value_label, '--').bind_text_from(
                    page.model, ('metrics', 'value'), backward=_value_text)
            with layout.cell('chart'):
                history_chart(page, value_label, self.id, 'value')
        with layout.cell('events'):
            self.ui.events_table(page)

        def update_colors():
            code = page.model.metrics.get('exit_code')
            if code is not None:
                exit_label.style(f"color: {STATUS_COLORS['online' if code == 0 else 'failed']}")
            if has_value:
                val = page.model.metrics.get('value')
                if val is not None:
                    value_label_widget.style(f"color: {STATUS_COLORS[level_for_value(val)]}")

        page.on_refresh(update_colors)
        page.start()


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
