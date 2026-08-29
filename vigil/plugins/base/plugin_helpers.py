"""Small pure helpers shared across plugins: resolve_secret (runs a
password_command on the Vigil host at construction so requests() stays pure),
the PluginConfigMixin that reads the shared config keys (id, interval,
ssh_config/target_host), the StatusAccumulator worst-of pattern, shell
utilities for probe scripts, and the level_for threshold check plus the
byte/duration/age formatting functions."""

import re
import shlex
import subprocess
from typing import Any, List, Optional

from vigil.core.settings.config_schema import PluginConfig
from vigil.core.connectors.types import Status


def resolve_secret(password: Optional[str],
                   password_command: Optional[str]) -> Optional[str]:
    """Resolve a plugin secret from either a literal value or a command run on
    the Vigil host. Used by HTTP plugins whose auth password can come from a
    `password_command` (e.g. a `pass`/`cat` invocation).

    Called at plugin construction (config-processing time on the Vigil host),
    which keeps the per-cycle `requests()` pure. HTTP plugins now fetch from
    Vigil's perspective, so a `password_command` runs here, not on the target.
    A rotated command output is picked up on restart. Returns None if neither
    is set, or if the command fails (the caller reports the auth failure)."""
    if password is not None:
        return password
    if not password_command:
        return None
    try:
        out = subprocess.run(password_command, shell=True, capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n")


class PluginConfigMixin:
    def _init_config(self, name: str, config: PluginConfig):
        self.name = name
        self.id = config.get('id', name)
        self.config = config
        self.interval = parse_duration(config.get('interval', 60))
        self.children: List[Any] = []
        ssh_cfg = config.get('ssh_config', {})
        self.target = ssh_cfg.get('host', config.get('target_host', 'localhost'))


class StatusAccumulator:
    """Escalating worst-of status plus the problems that caused it — the
    shared form of the per-plugin escalate-and-append pattern."""

    def __init__(self):
        self.status = Status.ONLINE
        self.problems: List[str] = []

    def escalate(self, status: str, problem: Optional[str] = None) -> None:
        """Raise the accumulated status if `status` is worse; record why."""
        if problem is not None:
            self.problems.append(problem)
        candidate = Status(status)
        if candidate.severity > self.status.severity:
            self.status = candidate

    @property
    def log_level(self) -> str:
        return self.status.log_level


SCRIPT_SEP = "@@VIGIL_SPLIT@@"


def password_line(password_command, password) -> str:
    """Shell line putting a plugin's secret in __pw, from a command or a literal."""
    if password_command:
        return f"__pw=$({password_command})"
    return f"__pw={shlex.quote(password)}"


def dq(value: str) -> str:
    """Double-quote a string for embedding inside a single-quoted shell trap."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def level_for(value: float, warning: float, threshold: float) -> str:
    """Map a numeric reading onto online/warning/failed by its two thresholds."""
    if value >= threshold:
        return 'failed'
    if value >= warning:
        return 'warning'
    return 'online'


def format_bytes(gb: float) -> str:
    """Format a size given in GB as a human-readable MB/GB/TB string."""
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
    """Parse a '2h30m'-style duration (or bare seconds) into seconds."""
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
    """Format seconds as the two largest whole units, e.g. '1 Day 2 Hours'."""
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
    """Format an age in seconds as a coarse 'N Days ago'-style string."""
    if seconds < 0:
        return 'Never'
    return f'{format_duration(seconds)} ago'
