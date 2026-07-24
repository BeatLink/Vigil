import re

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
