import re
from typing import Any, Dict, List


class PluginConfigMixin:
    def _init_config(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.id = config.get('id', name)
        self.config = config
        self.interval = parse_duration(config.get('interval', 60))
        self.children: List[Any] = []
        ssh_cfg = config.get('ssh_config', {})
        self.target = ssh_cfg.get('host', config.get('target_host', 'localhost'))


def level_for(value: float, warning: float, threshold: float) -> str:
    if value >= threshold:
        return 'failed'
    if value >= warning:
        return 'warning'
    return 'online'


def format_bytes(gb: float) -> str:
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{gb * 1024:.0f} MB"


_PARSE_UNITS = {
    'w': 7 * 24 * 3600,
    'd': 24 * 3600,
    'h': 3600,
    'm': 60,
    's': 1,
}

_FORMAT_UNITS = [
    (7 * 24 * 3600, 'Week'),
    (24 * 3600,     'Day'),
    (3600,          'Hour'),
    (60,            'Minute'),
    (1,             'Second'),
]


def parse_duration(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    matches = re.findall(r'(\d+)([wdhms])', value.lower())
    if not matches:
        raise ValueError(f"Unrecognised duration: {value!r}. Use e.g. '1w', '7d', '2h30m', '60s'.")
    return sum(int(n) * _PARSE_UNITS[u] for n, u in matches)


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return '0 Seconds'
    parts = []
    remaining = seconds
    for unit_secs, name in _FORMAT_UNITS:
        if remaining >= unit_secs:
            count = remaining // unit_secs
            remaining %= unit_secs
            parts.append(f'{count} {name}{"s" if count != 1 else ""}')
    return ' '.join(parts[:2])


def format_age(seconds: int) -> str:
    if seconds < 0:
        return 'Never'
    return f'{format_duration(seconds)} ago'
