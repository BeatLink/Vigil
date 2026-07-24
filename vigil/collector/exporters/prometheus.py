import re
from typing import Any

_INVALID = re.compile(r'[^a-zA-Z0-9_:]')

_STATUS_VALUE = {'online': 1.0, 'warning': 0.5, 'failed': 0.0, 'offline': -1.0}


def _sanitize_name(name: str) -> str:
    name = _INVALID.sub('_', name)
    if name and name[0].isdigit():
        name = '_' + name
    return name


def _escape_label(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def render(db: Any) -> str:
    lines = []

    lines.append('# HELP vigil_up Monitor status (1=online, 0.5=warning, 0=failed, -1=offline)')
    lines.append('# TYPE vigil_up gauge')
    for collector_id, state in sorted(db.latest_statuses().items()):
        val = _STATUS_VALUE.get(state, -1.0)
        lines.append(f'vigil_up{{monitor="{_escape_label(collector_id)}",state="{_escape_label(state)}"}} {val}')

    lines.append('# HELP vigil_metric Latest value of a collected metric')
    lines.append('# TYPE vigil_metric gauge')
    for m in db.latest_metrics():
        labels = (
            f'monitor="{_escape_label(m["collector"])}",'
            f'target="{_escape_label(m["target"])}",'
            f'metric="{_escape_label(m["metric_name"])}"'
        )
        lines.append(f'vigil_metric{{{labels}}} {m["value"]}')

    return '\n'.join(lines) + '\n'
