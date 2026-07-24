import re
from typing import Dict, Any, List, Optional

from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult
from vigil.plugins.base.web_plugin_base import UIPlugin
from vigil.plugins.base.plugin_helpers import level_for as _level_for

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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pattern = self.config.get('pattern')
        self.warning = self.config.get('warning')
        self.threshold = self.config.get('threshold')
        self.invert = bool(self.config.get('invert', False))
        self.value_label = self.config.get('value_label', 'VALUE')
        self.value_unit = self.config.get('value_unit', '')
        self.has_value = self.pattern is not None

        from vigil.core.ui.ui.spec import register_color_rule, register_formatter
        self._exit_color_name = f'command_exit_{self.id}'
        register_color_rule(self._exit_color_name)(
            lambda code: None if code is None else ('online' if code == 0 else 'failed'))
        self._value_color_name = f'command_value_{self.id}'
        register_color_rule(self._value_color_name)(self._value_color)
        self._value_format_name = f'command_value_fmt_{self.id}'
        register_formatter(self._value_format_name)(
            lambda v: '--' if v is None else f'{v:g}{self.value_unit}')

    def _value_color(self, value: Optional[float]) -> Optional[str]:
        if value is None:
            return None
        if self.warning is None or self.threshold is None:
            return 'online'
        if self.invert:
            return _level_for(-value, -float(self.warning), -float(self.threshold))
        return _level_for(value, float(self.warning), float(self.threshold))

    @property
    def UI_SPEC(self):
        cards = {
            'exit_card': {'metric': 'exit_code', 'title': 'EXIT CODE', 'format': 'int',
                          'color': self._exit_color_name},
        }
        spec = {
            'layout': _DEFAULT_LAYOUT_METRIC if self.has_value else _DEFAULT_LAYOUT_PLAIN,
            'cards': cards,
            'events': True,
        }
        if self.has_value:
            cards['value_card'] = {'metric': 'value', 'title': self.value_label,
                                   'format': self._value_format_name, 'color': self._value_color_name}
            spec['chart'] = {'metric': 'value', 'title': self.value_label}
        return spec

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.ui.spec import generic_render
        generic_render(self, context)


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
